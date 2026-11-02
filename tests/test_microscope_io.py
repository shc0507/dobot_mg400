"""Camera and host-data tests using generated images and temporary files."""

import io
import os
import stat
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError

import cv2
import numpy as np
import pytest

from cameras import uvc
from cameras.usb import enumerate_usb_cameras
from microscope import autofocus, paths
from microscope.aruco_locator import Locator, detect, marker_center
from microscope.autofocus import AutoFocuser, FocusMeasurement, focus_score
from microscope.objective_calibration import measure_image_displacement


@pytest.fixture
def data_roots(monkeypatch, tmp_path):
    calibration = tmp_path / "calibration"
    runs = tmp_path / "runs"
    monkeypatch.setenv("MG400_MICROSCOPE_CALIBRATION_DIR", str(calibration))
    monkeypatch.setenv("MG400_ORCH_RUNS_DIR", str(runs))
    return calibration, runs


def test_host_paths_are_lazy_and_outputs_are_private(data_roots):
    calibration, runs = data_roots
    assert paths.calibration_path("objective_views") == calibration / "objective_views"
    assert paths.calibration_path("current_objective.txt") == calibration / "current_objective.txt"
    assert (
        paths.calibration_path("objective_reference.png") == calibration / "objective_reference.png"
    )
    assert not calibration.exists() and not runs.exists()
    with paths.private_open(paths.run_path("microscope/curve.csv"), "x") as output:
        output.write("value\n")
    assert stat.S_IMODE(runs.stat().st_mode) & 0o027 == 0
    assert stat.S_IMODE((runs / "microscope/curve.csv").stat().st_mode) & 0o037 == 0
    with pytest.raises(FileExistsError):
        paths.private_open(paths.run_path("microscope/curve.csv"), "x")


@pytest.mark.parametrize("writer", [paths.directory, paths.private_open])
def test_every_writer_rejects_raw_parent_traversal(data_roots, writer):
    calibration, runs = data_roots
    with pytest.raises(ValueError, match="traversal"):
        writer(calibration / ".." / "escape")
    assert not calibration.exists() and not runs.exists()
    assert not (calibration.parent / "escape").exists()


def test_symlink_ancestor_above_configured_root_is_never_followed(data_roots, monkeypatch):
    calibration, _ = data_roots
    outside = calibration.parent / "outside"
    outside.mkdir()
    alias = calibration.parent / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("MG400_MICROSCOPE_CALIBRATION_DIR", str(alias / "newroot"))
    with pytest.raises(OSError):
        paths.private_open(alias / "newroot" / "newchild" / "image.png", "wb")
    assert list(outside.iterdir()) == []


def test_symlink_child_and_unsafe_root_refuse_before_creating_descendants(data_roots):
    calibration, _ = data_roots
    calibration.mkdir(mode=0o777)
    calibration.chmod(0o777)
    with pytest.raises(ValueError, match="private"):
        paths.private_open(calibration / "newchild" / "file.txt")
    assert not (calibration / "newchild").exists()
    calibration.chmod(0o750)
    outside = calibration.parent / "outside"
    outside.mkdir()
    (calibration / "alias").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        paths.private_open(calibration / "alias" / "newchild" / "file.txt")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public"])
def test_output_refusal_preserves_existing_file(data_roots, kind):
    calibration, _ = data_roots
    paths.directory(calibration)
    original = calibration / "original"
    original.write_bytes(b"preserve")
    original.chmod(0o640)
    target = calibration / "output"
    if kind == "symlink":
        target.symlink_to(original)
    elif kind == "hardlink":
        os.link(original, target)
    else:
        target = original
        original.chmod(0o644)
    with pytest.raises((OSError, ValueError)):
        paths.private_open(target)
    assert original.read_bytes() == b"preserve"


def jpeg():
    image = np.zeros((32, 40, 3), np.uint8)
    image[:, 20:] = 255
    return cv2.imencode(".jpg", image)[1].tobytes()


class Response(io.BytesIO):
    def __init__(self, data, url, status=200):
        super().__init__(data)
        self.url, self.status = url, status

    def geturl(self):
        return self.url


def test_go2rtc_reads_only_fixed_loopback_jpeg():
    calls = []

    def opener(url, timeout):
        calls.append((url, timeout))
        return Response(jpeg(), url)

    capture = uvc.Go2RTCCapture("camera2", opener=opener)
    assert calls == []
    ok, frame = capture.read()
    assert ok and frame.shape == (32, 40, 3)
    assert calls == [("http://127.0.0.1:1984/api/frame.jpeg?src=camera2", 2.0)]
    capture.release()
    assert capture.read() == (False, None)


@pytest.mark.parametrize(
    "stream", ["", "http://example.invalid", "../camera2", "camera2&src=other", "camera 2"]
)
def test_camera_stream_rejects_urls_and_injection(stream):
    with pytest.raises(ValueError):
        uvc.Go2RTCCapture(stream)
    with pytest.raises(ValueError):
        uvc.USBCamera(stream)


def test_default_opener_disables_proxies_and_redirects(monkeypatch):
    observed = []
    monkeypatch.setattr(
        uvc, "build_opener", lambda *handlers: observed.extend(handlers) or object()
    )
    uvc.Go2RTCCapture("camera2")
    assert observed[0].proxies == {}
    assert observed[1].redirect_request(None, None, 302, None, None, "http://bad.invalid") is None


@pytest.mark.parametrize("failure", ["status", "redirect", "oversized", "dimensions", "invalid"])
def test_go2rtc_refuses_bad_frames_before_decode(failure, monkeypatch):
    url = "http://127.0.0.1:1984/api/frame.jpeg?src=camera2"
    data, response_url, status = jpeg(), url, 200
    if failure == "status":
        status = 503
    elif failure == "redirect":
        response_url = "http://example.invalid/frame"
    elif failure == "oversized":
        data = b"x" * 16_777_217
    elif failure == "dimensions":
        data = b"\xff\xd8\xff\xc0\x00\x0b\x08\x13\x88\x13\x88\x01\x01\x11\x00"
    else:
        data = b"not JPEG"
    monkeypatch.setattr(cv2, "imdecode", lambda *_: pytest.fail("invalid data reached decoder"))
    response = Response(data, response_url, status)
    capture = uvc.Go2RTCCapture("camera2", opener=lambda *_args, **_kwargs: response)
    with pytest.raises(RuntimeError):
        capture.read()
    assert response.closed


class Frames:
    def __init__(self, shape=(32, 40, 3)):
        self.frame = np.zeros(shape, np.uint8)
        self.closed = False

    def read(self):
        return not self.closed, self.frame.copy()

    def release(self):
        self.closed = True


def test_side_camera_fresh_ids_copy_and_reopen(monkeypatch):
    monkeypatch.setattr(cv2, "VideoCapture", lambda *_: pytest.fail("native camera opened"))
    captures = []
    camera = uvc.USBCamera(
        "camera2",
        resolution=(40, 32),
        fps=60,
        capture_factory=lambda _: captures.append(Frames()) or captures[-1],
    )
    assert captures == []
    camera.open()
    try:
        first = camera.get_frame_packet()
        assert first is not None and first[1] <= time.monotonic()
        first[2][:] = 255
        assert not camera.get_frame_packet(wait=False)[2].any()
        second = camera.get_frame_packet(after_id=first[0])
        assert second[0] > first[0] and second[1] >= first[1]
        camera.close()
        assert captures[0].closed
        camera.open()
        third = camera.get_frame_packet(after_id=second[0])
        assert third[0] > second[0]
    finally:
        camera.close()


def test_side_camera_mismatched_reference_size_is_an_error():
    camera = uvc.USBCamera("camera2", resolution=(3840, 2160), capture_factory=lambda _: Frames())
    camera.open()
    try:
        with pytest.raises(RuntimeError, match="acquisition stopped"):
            camera.get_frame_packet()
    finally:
        camera.close()


def test_linux_camera_identity_uses_hub_path_without_opening_device(tmp_path):
    sys_root, by_path, by_id = (tmp_path / name for name in ("sys", "by-path", "by-id"))
    for directory in (sys_root, by_path, by_id):
        directory.mkdir()
    node = sys_root / "video7"
    node.mkdir()
    (node / "name").write_text("Webcam AC410")
    (by_path / "hub-camera").symlink_to("/dev/video7")
    (by_id / "shared-serial").symlink_to("/dev/video7")
    (device,) = enumerate_usb_cameras(sys_root=sys_root, by_path=by_path, by_id=by_id)
    assert device.is_ac410 and device.source == str(by_path / "hub-camera")
    assert device.device_id == str(by_path / "hub-camera")


def test_continuous_scan_uses_dispatch_time_after_delayed_ack(monkeypatch):
    ticket = SimpleNamespace(started=100.0, started_wall=1000.0)
    motor = SimpleNamespace(focus_move_async=Mock(return_value=ticket), wait_focus_done=Mock())
    camera = SimpleNamespace(get_frame_packet=Mock(side_effect=AssertionError("already elapsed")))
    focuser = AutoFocuser(camera, motor, speed=100)
    focuser._move_to = Mock()
    focuser._measure = Mock(
        side_effect=[FocusMeasurement(0, 1, "coarse"), FocusMeasurement(10, 2, "coarse")]
    )
    focuser._wait = Mock()
    monkeypatch.setattr(
        autofocus, "time", SimpleNamespace(monotonic=lambda: 100.2, time=lambda: 1000.2)
    )
    result = focuser._continuous_scan(0, 10, "coarse")
    assert len(result) == 2
    camera.get_frame_packet.assert_not_called()
    motor.wait_focus_done.assert_called_once_with(ticket, cancel_event=focuser.cancel_event)
    assert focuser.position == 10


def test_continuous_scan_retains_wall_clock_position_estimates(monkeypatch):
    ticket = SimpleNamespace(started=100.0, started_wall=1000.0)
    motor = SimpleNamespace(focus_move_async=Mock(return_value=ticket), wait_focus_done=Mock())
    frame = np.zeros((32, 32, 3), np.uint8)
    camera = SimpleNamespace(get_frame_packet=Mock(return_value=(1, 1000.5, frame)))
    focuser = AutoFocuser(camera, motor, speed=100)
    focuser._move_to = Mock()
    focuser._measure = Mock(
        side_effect=[FocusMeasurement(0, 1, "coarse"), FocusMeasurement(100, 2, "coarse")]
    )
    focuser._wait = Mock()
    ticks = iter((100.3, 101.1))
    monkeypatch.setattr(
        autofocus, "time", SimpleNamespace(monotonic=lambda: next(ticks), time=lambda: 1001.1)
    )
    result = focuser._continuous_scan(0, 100, "coarse")
    assert result[1].position == 50 and result[1].captured_at == 1000.5
    assert motor.wait_focus_done.call_count == 1


def test_image_algorithms_preserve_sharpness_displacement_and_marker_policy():
    rng = np.random.default_rng(11)
    sharp = rng.integers(0, 256, (160, 160), dtype=np.uint8)
    blurred = cv2.GaussianBlur(sharp, (15, 15), 3)
    assert focus_score(sharp) > focus_score(blurred)
    corners = np.array([[10, 20], [30, 20], [30, 40], [10, 40]], np.float32)
    assert marker_center(corners) == pytest.approx([20, 30])
    assert detect(np.zeros((100, 100), np.uint8))["status"] == "black_frame"
    locator = object.__new__(Locator)
    locator.near_px = 5
    locator.references = {0: {"center": [20, 30], "shape": [100, 100]}}
    result = locator.classify(
        {"shape": [100, 100], "status": "ok", "markers": [{"id": 0, "center": [22, 30]}]}
    )
    assert result["markers"][0]["dx_px"] == 2
    assert (
        locator.classify({"shape": [50, 100], "markers": [{"id": 0}]})["status"]
        == "image_size_mismatch"
    )
    shifted = np.roll(sharp, 4, axis=1)
    displacement = measure_image_displacement(sharp, shifted)
    assert displacement is not None


def test_go2rtc_http_error_response_is_closed():
    body = io.BytesIO(b"unavailable")
    error = HTTPError("http://127.0.0.1:1984", 503, "unavailable", {}, body)

    def unavailable(*_args, **_kwargs):
        raise error

    capture = uvc.Go2RTCCapture("camera2", opener=unavailable)
    with pytest.raises(RuntimeError, match="not successful"):
        capture.read()
    assert body.closed


def test_side_camera_refuses_reopen_until_old_reader_exits(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class BlockedFrames(Frames):
        def read(self):
            entered.set()
            assert release.wait(1)
            return True, self.frame.copy()

    captures = [BlockedFrames(), Frames()]
    opened = []

    def factory(_stream):
        opened.append(captures[len(opened)])
        return opened[-1]

    camera = uvc.USBCamera("camera2", resolution=(40, 32), capture_factory=factory)
    camera.open()
    old_thread = camera._thread
    join = old_thread.join
    try:
        assert entered.wait(1)
        # Exercise the shutdown deadline without spending three seconds in it.
        monkeypatch.setattr(old_thread, "join", lambda **_: None)
        with pytest.raises(RuntimeError, match="still stopping"):
            camera.close()
        with pytest.raises(RuntimeError, match="still stopping"):
            camera.open()
        assert len(opened) == 1
        release.set()
        join(1)
        assert not old_thread.is_alive()
        assert camera._frame_id == 0 and camera._latest_frame is None
        camera.open()
        packet = camera.get_frame_packet()
        assert packet[0] == 1 and len(opened) == 2
    finally:
        release.set()
        join(1)
        camera.close()
