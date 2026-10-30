"""Vision service checks use fixtures, fake HTTP, and fake GPU runtimes only."""

import io
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from contract_helpers import assert_error, check_contract
from PIL import Image

from vision import __main__ as entrypoint
from vision import frames as frame_module
from vision import service as service_module
from vision.frames import DemoSource, FrameError, Go2RTCSource
from vision.qr import DetectionCancelled, QRCode
from vision.service import DemoLocalizer, VisionService, create_app

TOKEN = "v" * 24
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def make_service():
    services = []

    def make(**options):
        service = VisionService(demo=options.pop("demo", True), **options)
        services.append(service)
        service.start()
        return service

    yield make
    for service in services:
        service.close(wait_s=0.5)


def completed(service, command_id):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        state = service.snapshot()
        if state["last_command"] and state["last_command"]["id"] == command_id:
            return state
        threading.Event().wait(0.005)
    pytest.fail("Inference did not finish")


def jpeg():
    encoded = io.BytesIO()
    Image.new("RGB", (16, 12), "white").save(encoded, format="JPEG")
    return encoded.getvalue()


@pytest.mark.parametrize("camera", [True, 0, 4, -1, "1", 1.0, None])
def test_invalid_camera_never_opens_a_source(camera):
    opener = Mock(side_effect=AssertionError("No source access"))
    with pytest.raises(ValueError):
        Go2RTCSource(opener=opener).read(camera)
    opener.assert_not_called()


def test_fixed_frame_url_timeout_short_reads_and_rgb():
    class ShortReads(io.BytesIO):
        def read(self, size):
            return super().read(min(size, 7))

        read1 = read

    stream = ShortReads(jpeg())
    opener = Mock(return_value=stream)
    image = Go2RTCSource(opener=opener, timeout=0.75).read(2)
    opener.assert_called_once_with("http://127.0.0.1:1984/api/frame.jpeg?src=camera2", timeout=0.75)
    assert image.size == (16, 12) and image.mode == "RGB"
    assert stream.closed


@pytest.mark.parametrize("content", [b"", b"not an image", jpeg()[:-20]])
def test_invalid_or_truncated_jpeg_is_rejected(content):
    with pytest.raises(FrameError):
        Go2RTCSource(opener=lambda *a, **k: io.BytesIO(content)).read(1)


def test_frame_byte_and_pixel_limits():
    content = jpeg()
    assert Go2RTCSource(max_bytes=len(content), opener=lambda *a, **k: io.BytesIO(content)).read(
        1
    ).size == (16, 12)
    with pytest.raises(FrameError, match="byte limit"):
        Go2RTCSource(max_bytes=len(content) - 1, opener=lambda *a, **k: io.BytesIO(content)).read(1)
    oversized = bytearray(content)
    start = oversized.index(b"\xff\xc0")
    oversized[start + 5 : start + 7] = (5000).to_bytes(2, "big")
    oversized[start + 7 : start + 9] = (4000).to_bytes(2, "big")
    with pytest.raises(FrameError, match="pixel limit"):
        Go2RTCSource(opener=lambda *a, **k: io.BytesIO(oversized)).read(1)


def test_timeout_and_unexpected_response_endpoint():
    with pytest.raises(FrameError, match="timed out"):
        Go2RTCSource(opener=Mock(side_effect=TimeoutError("timed out"))).read(1)
    stream = io.BytesIO(jpeg())
    stream.geturl = lambda: "http://other.example/frame"
    with pytest.raises(FrameError, match="different endpoint"):
        Go2RTCSource(opener=lambda *a, **k: stream).read(1)


def test_frame_body_deadline_stops_trickling_single_reads():
    now = [0.0]
    reads = []

    class Trickle(io.BytesIO):
        def read(self, size):
            raise AssertionError("A fill-sized read can hide multiple blocking socket reads")

        def read1(self, size):
            reads.append(size)
            now[0] += 0.4
            return super().read(min(size, 16))

    stream = Trickle(jpeg())
    with pytest.raises(FrameError, match="body read timed out"):
        Go2RTCSource(opener=lambda *a, **k: stream, timeout=1, clock=lambda: now[0]).read(1)
    assert len(reads) == 3 and stream.closed


def test_frame_body_deadline_refuses_late_complete_image():
    now = [0.0]

    class Late(io.BytesIO):
        def read1(self, size):
            now[0] = 2
            return super().read(size)

    with pytest.raises(FrameError, match="body read timed out"):
        Go2RTCSource(opener=lambda *a, **k: Late(jpeg()), timeout=1, clock=lambda: now[0]).read(1)


def test_default_frame_opener_rejects_redirect_before_second_request(monkeypatch):
    from email.message import Message
    from urllib.request import HTTPHandler, build_opener
    from urllib.response import addinfourl

    requested = []

    class FakeHTTP(HTTPHandler):
        def http_open(self, request):
            requested.append(request.full_url)
            assert len(requested) == 1
            headers = Message()
            headers["Location"] = "http://untrusted.example/frame"
            response = addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
            response.msg = "Found"
            return response

    monkeypatch.setattr(
        frame_module, "build_opener", lambda *handlers: build_opener(*handlers, FakeHTTP())
    )
    with pytest.raises(FrameError, match="HTTP 302"):
        Go2RTCSource().read(1)
    assert requested == ["http://127.0.0.1:1984/api/frame.jpeg?src=camera1"]


def test_demo_source_missing_and_valid_fixture(tmp_path):
    with pytest.raises(FrameError):
        DemoSource(tmp_path).read(1)
    assert DemoSource().read(3).size == (96, 64)


@pytest.mark.parametrize("token", [None, TOKEN])
@pytest.mark.parametrize("base_path", ["/", "/vision/"])
def test_contract_conformance(make_service, token, base_path):
    now = [10.0]
    service = make_service(clock=lambda: now[0])
    app = create_app(service, token=token, base_path=base_path)
    check_contract(
        app.test_client(),
        advance=lambda seconds: now.__setitem__(0, now[0] + seconds),
        expiry_count=lambda: service.expiry_count,
        base_path=base_path,
        headers={"Authorization": f"Bearer {token}"} if token else {},
    )


def test_demo_start_is_explicit_idempotent_and_needs_no_torch(monkeypatch):
    loader = Mock(side_effect=AssertionError("No torch in demo"))
    factory = Mock(side_effect=lambda: DemoLocalizer())
    service = VisionService(demo=True, torch_loader=loader, localizer_factory=factory)
    assert not service.snapshot()["model_loaded"]
    assert service._poller is None
    with pytest.raises(Exception, match="not started"):
        service.read("detect", 1)
    try:
        service.start()
        service.start()
        assert service.snapshot()["model_loaded"] and service.snapshot()["device"] == "demo"
        factory.assert_called_once()
        loader.assert_not_called()
    finally:
        service.close()
    assert not service._poller.is_alive()


@pytest.mark.parametrize(
    "available,requested,expected",
    [(True, None, "cuda"), (False, None, "cpu"), (True, "cuda:1", "cuda:1"), (True, "cpu", "cpu")],
)
def test_gpu_fraction_precedes_load_and_single_warmup(make_service, available, requested, expected):
    order = []
    cuda = SimpleNamespace(
        is_available=lambda: available,
        set_per_process_memory_fraction=lambda fraction, **kw: order.append(("cap", fraction, kw)),
    )
    model = SimpleNamespace(predict=lambda image: order.append(("warmup", image.size)))

    def factory(weights, **options):
        order.append(("load", weights, options))
        return model

    source = Mock()
    source.read.side_effect = AssertionError("Warm-up must not fetch a camera")
    service = make_service(
        demo=False,
        device=requested,
        gpu_fraction=0.15,
        frames=source,
        torch_loader=lambda: SimpleNamespace(cuda=cuda),
        localizer_factory=factory,
    )
    expected_order = [("load", service.weights, {"device": expected}), ("warmup", (640, 640))]
    if expected.startswith("cuda"):
        expected_order.insert(0, ("cap", 0.15, {"device": 1 if expected.endswith(":1") else 0}))
    assert order == expected_order
    assert not service.snapshot()["connected"]


def test_unavailable_cuda_fails_before_model_load():
    factory = Mock(side_effect=AssertionError("No model load"))
    service = VisionService(
        device="cuda",
        torch_loader=lambda: SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)),
        localizer_factory=factory,
    )
    with pytest.raises(ValueError, match="unavailable"):
        service.start()
    assert not service.snapshot()["model_loaded"] and not service.snapshot()["busy"]
    factory.assert_not_called()
    with pytest.raises(RuntimeError, match="closed"):
        service.start()
    service.close()


@pytest.mark.parametrize("fraction", [0, -0.1, 1.01, float("nan"), float("inf"), True, "0.1"])
def test_invalid_gpu_fraction_is_rejected(fraction):
    with pytest.raises(ValueError):
        VisionService(gpu_fraction=fraction)


def test_read_routes_return_pixels_and_annotated_jpeg_without_a_lease(make_service):
    service = make_service()
    client = create_app(service).test_client()
    response = client.get("/api/detect?camera=2")
    assert response.status_code == 200
    result = response.get_json()
    assert result["camera"] == 2 and result["counts"] == {"Chip": 1, "Robotic Arm": 1}
    assert result["image_size"] == {"width": 96, "height": 64}
    assert result["calibrated"] is False and "robot_mm" not in str(result)
    assert service.snapshot()["calibrated_cameras"] == []
    assert service.lease.holder is None
    annotated = client.get("/api/annotated.jpg?camera=2")
    assert annotated.status_code == 200 and annotated.mimetype == "image/jpeg"
    assert Image.open(io.BytesIO(annotated.data)).size == (96, 64)
    assert annotated.headers["Cache-Control"] == "no-store"
    assert annotated.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("route", ["detect", "labels", "annotated.jpg"])
@pytest.mark.parametrize(
    "query",
    ["", "?camera=0", "?camera=1&camera=2", "?camera=1&url=http://example.com", "?camera=true"],
)
def test_bad_read_queries_access_no_frame(make_service, route, query):
    source = Mock()
    source.read.side_effect = AssertionError("No frame for invalid query")
    service = make_service(frames=source)
    assert_error(create_app(service).test_client().get(f"/api/{route}{query}"), 400)
    source.read.assert_not_called()


@pytest.mark.parametrize("route", ["detect", "labels", "annotated.jpg"])
def test_read_routes_share_token_host_origin_and_forwarded_checks(make_service, route):
    source = Mock()
    source.read.side_effect = AssertionError("No frame for unauthorized request")
    service = make_service(frames=source)
    path = f"/api/{route}?camera=1"
    client = create_app(service).test_client()
    assert_error(client.get(path, headers={"Host": "evil.example"}), 403)
    assert_error(client.get(path, headers={"X-Forwarded-For": ""}), 403)
    assert_error(client.get(path, headers={"Origin": "null"}), 403)
    secured = create_app(service, token=TOKEN).test_client()
    assert_error(secured.get(path), 401)
    assert_error(
        secured.get(
            path, headers={"Authorization": f"Bearer {TOKEN}", "Origin": "http://other.example"}
        ),
        403,
    )
    source.read.assert_not_called()


def test_labels_decode_existing_qr_fixture(make_service):
    with Image.open(ROOT / "tests/fixtures/qr/image2-stamp-1.png") as image:
        fixture = image.convert("RGB")
    service = make_service(frames=SimpleNamespace(read=lambda camera: fixture.copy()))
    response = create_app(service).test_client().get("/api/labels?camera=1")
    assert response.status_code == 200
    result = response.get_json()
    assert any(code["text"] == "Stamp" and code["decoded"] for code in result["labels"])
    assert result["image_size"] == {"width": fixture.width, "height": fixture.height}
    assert result["calibrated"] is False


def test_label_region_maps_coordinates_back_and_command_completes(make_service):
    calls = []

    def qr(image, **options):
        calls.append(image.shape)
        return [QRCode(((1, 2), (5, 2), (5, 6), (1, 6)), text="located", decoded=False)]

    service = make_service(qr_detector=qr)
    client = create_app(service).test_client()
    headers = {"X-MG400-Client": "tester"}
    assert_error(
        client.post("/api/command", headers=headers, json={"action": "read_labels", "camera": 1}),
        409,
    )
    assert client.post("/api/heartbeat", headers=headers, json={}).status_code == 200
    response = client.post(
        "/api/command",
        headers=headers,
        json={"action": "read_labels", "camera": 1, "region": [10, 20, 30, 40]},
    )
    assert response.status_code == 202
    state = completed(service, response.get_json()["command_id"])
    assert state["last_command"]["result"] == "ok" and state["last_result"]["count"] == 1
    assert calls == [(20, 20, 3)]
    cropped = state["last_command"]["output"]["labels"][0]
    assert cropped["center_px"] == [13, 24]
    assert cropped["bounds_px"] == [11, 22, 15, 26]
    assert cropped["corners_px"][0] == [11, 22]
    full = client.get("/api/labels?camera=1").get_json()["labels"][0]
    assert full["decoded"] is False and full["center_px"] == [3, 4]


def test_frame_failure_is_reported_without_stale_success(make_service):
    service = make_service(
        frames=SimpleNamespace(read=Mock(side_effect=FrameError("camera unavailable")))
    )
    client = create_app(service).test_client()
    assert_error(client.get("/api/detect?camera=1"), 503)
    state = service.snapshot()
    assert not state["connected"] and not state["busy"] and state["last_result"] is None
    assert state["fault"]["code"] == "vision_failed"


@pytest.mark.parametrize("expire", [False, True])
def test_stop_or_expiry_keeps_inference_serial_and_discards_late_result(make_service, expire):
    entered, release = threading.Event(), threading.Event()
    now = [10.0]
    model = DemoLocalizer()
    original = model.predict
    calls = []

    def predict(image):
        calls.append(image.size)
        if len(calls) > 1:
            entered.set()
            assert release.wait(2)
        return original(image)

    model.predict = predict
    service = make_service(localizer_factory=lambda: model, clock=lambda: now[0])
    client = create_app(service).test_client()
    headers = {"X-MG400-Client": "owner"}
    client.post("/api/heartbeat", headers=headers, json={})
    command = client.post("/api/command", headers=headers, json={"action": "detect", "camera": 1})
    try:
        assert entered.wait(1)
        assert_error(client.get("/api/labels?camera=2"), 409)
        assert_error(
            client.post("/api/command", headers=headers, json={"action": "detect", "camera": 2}),
            409,
        )
        if expire:
            now[0] += 3
            service.lease.poll()
            assert service.expiry_count == 1
        stop = client.post("/api/stop", headers={"X-MG400-Client": "other"}, json={"wait_s": 0})
        assert stop.status_code == 200 and stop.get_json()["stop"] == "pending"
    finally:
        release.set()
    state = completed(service, command.get_json()["command_id"])
    assert state["last_command"]["result"] == "stopped" and state["last_result"] is None
    assert state["last_command"]["output"] is None
    assert not state["busy"] and len(calls) == 2
    assert service.stop("other") == "confirmed"


@pytest.mark.parametrize(
    "environ,argv",
    [
        ({"MG400_VISION_TOKEN": "short"}, []),
        ({}, ["--bind", "0.0.0.0"]),
        ({"MG400_VISION_PORT": "bad"}, []),
        ({}, ["--port", "0"]),
        ({}, ["--gpu-fraction", "nan"]),
        ({}, ["--device", "bogus"]),
        ({}, ["--frame-timeout", "0"]),
    ],
)
def test_entrypoint_rejects_bad_configuration_before_serving(environ, argv):
    server = Mock(side_effect=AssertionError("No server"))
    with pytest.raises(SystemExit) as exit_:
        entrypoint.main(argv, environ=environ, server_factory=server)
    assert exit_.value.code == 2
    server.assert_not_called()


def test_demo_entrypoint_startup_signal_shutdown_and_defaults():
    service = VisionService(demo=True)
    captured = {}
    server = SimpleNamespace(
        run=lambda: signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None), close=Mock()
    )
    previous = signal.getsignal(signal.SIGTERM)

    def factory(app, **options):
        captured.update(options)
        assert not service.snapshot()["model_loaded"]
        return server

    assert (
        entrypoint.main(
            ["--demo"], environ={}, service_factory=lambda **kwargs: service, server_factory=factory
        )
        == 0
    )
    assert captured["host"] == "127.0.0.1" and captured["port"] == 8771
    assert captured["clear_untrusted_proxy_headers"] is False
    assert captured["max_request_body_size"] == 4097
    assert service._closed and not service._poller.is_alive()
    server.close.assert_called_once()
    assert signal.getsignal(signal.SIGTERM) == previous


@pytest.mark.parametrize(
    "region", [None, [], [1, 2, 3], [0, 0, 0, 2], [-1, 0, 2, 2], [True, 0, 2, 2], [0, 0, 3.0, 4]]
)
def test_invalid_region_does_not_access_frames(make_service, region):
    frames = SimpleNamespace(read=Mock(side_effect=AssertionError("No frame for invalid region")))
    service = make_service(frames=frames)
    client = create_app(service).test_client()
    headers = {"X-MG400-Client": "a"}
    client.post("/api/heartbeat", headers=headers, json={})
    assert_error(
        client.post(
            "/api/command",
            headers=headers,
            json={"action": "read_labels", "camera": 1, "region": region},
        ),
        400,
    )
    frames.read.assert_not_called()


def test_region_outside_frame_fails_without_qr_access(make_service):
    qr = Mock(side_effect=AssertionError("No QR outside the frame"))
    service = make_service(qr_detector=qr)
    client = create_app(service).test_client()
    headers = {"X-MG400-Client": "a"}
    client.post("/api/heartbeat", headers=headers, json={})
    response = client.post(
        "/api/command",
        headers=headers,
        json={"action": "read_labels", "camera": 1, "region": [0, 0, 100, 100]},
    )
    state = completed(service, response.get_json()["command_id"])
    assert state["last_command"]["result"] == "failed"
    assert state["last_command"]["output"] is None
    qr.assert_not_called()


def test_qr_stop_is_cooperative_and_any_client_can_wait(make_service):
    entered = threading.Event()

    def qr(image, *, cancelled):
        entered.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if cancelled():
                raise DetectionCancelled()
            threading.Event().wait(0.005)
        raise AssertionError("QR cancellation was not observed")

    service = make_service(qr_detector=qr)
    client = create_app(service).test_client()
    headers = {"X-MG400-Client": "owner"}
    client.post("/api/heartbeat", headers=headers, json={})
    command = client.post(
        "/api/command", headers=headers, json={"action": "read_labels", "camera": 1}
    )
    assert entered.wait(1)
    response = client.post("/api/stop", headers={"X-MG400-Client": "other"}, json={"wait_s": 1})
    assert response.get_json()["stop"] == "confirmed"
    assert (
        completed(service, command.get_json()["command_id"])["last_command"]["result"] == "stopped"
    )


def test_expiry_polls_without_http_requests(make_service):
    now = [0.0]
    service = make_service(clock=lambda: now[0])
    service.lease.acquire("owner")
    now[0] = 3
    deadline = time.monotonic() + 1
    while service.expiry_count == 0 and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    assert service.expiry_count == 1
    assert service.lease.snapshot()["holder"] is None


def test_imports_and_construction_have_no_io_or_detector_runtime(tmp_path):
    script = """
import builtins
import socket
import threading
from pathlib import Path
original_import = builtins.__import__
def reject_runtime(name, *args, **kwargs):
    if name.split('.')[0] in {'torch', 'ultralytics'}:
        raise AssertionError(name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = reject_runtime
socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(AssertionError('network'))
threads = len(threading.enumerate())
from vision import frames, service, __main__
instance = service.VisionService(demo=True)
assert not instance.snapshot()['model_loaded']
assert len(threading.enumerate()) == threads
assert not list(Path.cwd().iterdir())
"""
    subprocess.run([sys.executable, "-c", script], cwd=tmp_path, check=True)


def test_entrypoint_closes_server_after_failed_model_startup():
    service = VisionService(
        demo=True, localizer_factory=Mock(side_effect=RuntimeError("broken model"))
    )
    server = SimpleNamespace(run=Mock(), close=Mock())
    with pytest.raises(SystemExit) as exit_:
        entrypoint.main(
            ["--demo"],
            environ={},
            service_factory=lambda **kwargs: service,
            server_factory=lambda *a, **kw: server,
        )
    assert exit_.value.code == 2
    assert service._closed and not service.snapshot()["busy"]
    server.run.assert_not_called()
    server.close.assert_called_once()


@pytest.mark.parametrize("shutdown", [False, True])
def test_stop_during_warmup_is_pending_and_discards_model(shutdown):
    entered, release = threading.Event(), threading.Event()
    errors = []

    def predict(image):
        entered.set()
        assert release.wait(2)

    service = VisionService(demo=True, localizer_factory=lambda: SimpleNamespace(predict=predict))

    def startup():
        try:
            service.start()
        except RuntimeError as error:
            errors.append(str(error))

    thread = threading.Thread(target=startup)
    thread.start()
    try:
        assert entered.wait(1)
        result = service.close(0) if shutdown else service.stop("any-client", 0)
        assert result == "pending"
        assert service.snapshot()["busy"]
        assert not service.snapshot()["model_loaded"]
    finally:
        release.set()
        thread.join(2)
        service.close(0)
    assert not thread.is_alive() and errors == ["Vision startup was cancelled."]
    assert not service.snapshot()["busy"] and service.stop("any-client") == "confirmed"
    assert service._poller is None
    with pytest.raises(RuntimeError, match="closed"):
        service.start()


def test_close_during_poller_dispatch_never_joins_unstarted_thread(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real_start = threading.Thread.start
    errors = []
    service = VisionService(demo=True)

    def paused_start(thread):
        if thread.name == "vision-lease":
            entered.set()
            assert release.wait(2)
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", paused_start)

    def startup():
        try:
            service.start()
        except RuntimeError as error:
            errors.append(str(error))

    starter = threading.Thread(target=startup)
    starter.start()
    try:
        assert entered.wait(1)
        assert service.close(0) == "pending"
    finally:
        release.set()
        starter.join(2)
        service.close(0)
    assert errors == ["Vision startup was cancelled."]
    assert not service._poller.is_alive() and not service.snapshot()["model_loaded"]


def test_failed_inference_dispatch_finishes_reservation_and_allows_retry(make_service, monkeypatch):
    service = make_service()
    client = create_app(service).test_client()
    headers = {"X-MG400-Client": "owner"}
    client.post("/api/heartbeat", headers=headers, json={})
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", Mock(side_effect=RuntimeError("no threads")))
        assert_error(
            client.post("/api/command", headers=headers, json={"action": "detect", "camera": 1}),
            503,
        )
    state = service.snapshot()
    assert not state["busy"] and state["last_command"]["result"] == "failed"
    assert state["last_command"]["output"] is None
    assert service.stop("any-client") == "confirmed"
    assert client.get("/api/detect?camera=1").status_code == 200


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), "x" * (256 * 1024)])
def test_invalid_or_oversized_metadata_is_not_published(make_service, invalid):
    model = DemoLocalizer()
    original = model.predict

    def predict(image):
        annotated, result = original(image)
        result["extra"] = invalid
        return annotated, result

    model.predict = predict
    service = make_service(localizer_factory=lambda: model)
    client = create_app(service).test_client()
    assert_error(client.get("/api/detect?camera=1"), 503)
    headers = {"X-MG400-Client": "owner"}
    client.post("/api/heartbeat", headers=headers, json={})
    response = client.post("/api/command", headers=headers, json={"action": "detect", "camera": 1})
    state = completed(service, response.get_json()["command_id"])
    assert state["last_command"]["result"] == "failed"
    assert state["last_command"]["output"] is None and state["last_result"] is None
    assert state["fault"] and not state["busy"]
    assert len(client.get("/api/state").data) < service_module.MAX_RESULT_BYTES


def test_entrypoint_closes_server_even_when_service_close_fails():
    service = VisionService(demo=True)
    original_close = service.close

    def close():
        original_close()
        raise RuntimeError("close failed")

    service.close = close
    server = SimpleNamespace(run=Mock(), close=Mock())
    with pytest.raises(RuntimeError, match="close failed"):
        entrypoint.main(
            ["--demo"],
            environ={},
            service_factory=lambda **kwargs: service,
            server_factory=lambda *a, **kw: server,
        )
    server.close.assert_called_once()
