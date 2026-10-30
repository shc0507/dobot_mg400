import csv
import ctypes
import hashlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest

from toupcam_camera import MicroscopeCamera
from toupcam_camera.camera import _RecordingSession
from toupcam_camera.capture import ToupcamCapture, selection


class CameraRecordingTests(unittest.TestCase):
    def test_default_time_lapse_accepts_disabled_free_space_threshold(self):
        camera = MicroscopeCamera()
        camera._source = object()
        frame = np.zeros((2, 2, 3), dtype=np.uint8)
        writer = Mock()
        writer.isOpened.return_value = True

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(camera, "get_frame", return_value=frame),
                patch("toupcam_camera.camera.cv2.VideoWriter", return_value=writer),
            ):
                camera.start_recording(
                    Path(tmp) / "video.mp4",
                    minimum_free_gb=None,
                )

                self.assertEqual(camera.recording_mode, "time_lapse")
                self.assertEqual(camera.recording_interval_seconds, 30.0)
                self.assertEqual(camera.recording_fps, 1.0)
                camera.stop_recording()


if __name__ == "__main__":
    unittest.main()


class FakeHandle:
    def __init__(self, sdk, identity):
        self.sdk, self.identity = sdk, identity
        self.size = (2, 2)
        self.closed = self.stopped = 0
        self.calls = []
        self.frame = np.zeros((2, 2, 3), dtype=np.uint8)

    def SerialNumber(self):
        return self.sdk.serials[self.identity]

    def put_eSize(self, mode):
        self.size = self.sdk.modes[mode]

    def get_Size(self):
        return self.size

    def put_Speed(self, speed):
        self.calls.append(("speed", speed))
        if self.sdk.optional_error:
            raise RuntimeError("Optional speed unavailable")

    def put_Option(self, key, value):
        self.calls.append((key, value))
        if self.sdk.optional_error and key == self.sdk.TOUPCAM_OPTION_FRAMERATE:
            raise RuntimeError("Optional framerate unavailable")

    def StartPullModeWithCallback(self, callback, context):
        self.callback, self.context = callback, context

    def PullImageV4(self, buffer, still, bits, pitch, info):
        assert (still, bits, pitch, info) == (0, 24, -1, None)
        ctypes.memmove(buffer, self.frame.tobytes(), self.frame.nbytes)

    def emit(self, value):
        self.frame = np.full((self.size[1], self.size[0], 3), value, dtype=np.uint8)
        self.callback(self.sdk.TOUPCAM_EVENT_IMAGE, self.context)

    def Stop(self):
        self.stopped += 1

    def Close(self):
        self.closed += 1


class FakeSDK:
    TOUPCAM_OPTION_FRAMERATE = 1
    TOUPCAM_OPTION_BYTEORDER = 2
    TOUPCAM_EVENT_IMAGE = 4
    TOUPCAM_EVENT_ERROR = 8
    TOUPCAM_EVENT_DISCONNECTED = 16

    def __init__(self, *, optional_error=False):
        self.optional_error = optional_error
        self.modes = [(2, 2), (128, 128)]
        self.serials = {"one": "SERIAL-ONE", "two": "SERIAL-TWO"}
        model = SimpleNamespace(
            maxspeed=3, res=[SimpleNamespace(width=w, height=h) for w, h in self.modes]
        )
        self.devices = [
            SimpleNamespace(id=name, displayname=name, model=model) for name in self.serials
        ]
        self.handles = []
        self.Toupcam = SimpleNamespace(EnumV2=lambda: self.devices, Open=self.open)

    def open(self, identity):
        handle = FakeHandle(self, identity)
        self.handles.append(handle)
        return handle

    def capture(self, *args, **kwargs):
        return ToupcamCapture(*args, **kwargs, sdk=self)


class Writer:
    def __init__(self):
        self.frames = []
        self.released = False

    def isOpened(self):
        return True

    def write(self, frame):
        self.frames.append(frame.copy())

    def release(self):
        self.released = True


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("The fake camera did not reach the expected state")


def test_imports_and_constructor_do_not_load_library_open_devices_or_start_workers():
    script = """
import cv2
import numpy
import ctypes
import threading
import sys

def forbidden(*args, **kwargs):
    raise AssertionError('Unexpected native loading or worker startup')

ctypes.cdll.LoadLibrary = forbidden
threading.Thread.start = forbidden
import toupcam_camera
camera = toupcam_camera.MicroscopeCamera(settings={})
assert not camera.is_open
assert 'toupcam_camera.toupcam' not in sys.modules
import toupcam_camera.toupcam
assert toupcam_camera.toupcam.Toupcam._Toupcam__lib is None
"""
    subprocess.run([sys.executable, "-c", script], check=True, timeout=15)


def test_vendor_wrapper_is_the_exact_pinned_version_with_lf_only():
    path = Path(__file__).resolve().parents[1] / "src/toupcam_camera/toupcam.py"
    contents = path.read_bytes()
    assert contents.startswith(b'"""Version: 59.30594.20260120\n')
    assert b"\r" not in contents
    assert (
        hashlib.sha256(contents).hexdigest()
        == "c5b099d6559b73b5c0d0da6398d7f02bf26058abc1e09ef7fb6bb67ac888fd91"
    )


@pytest.mark.parametrize("optional_error", [False, True])
def test_capture_keeps_fastest_readout_and_latest_bgr_frame(optional_error):
    sdk = FakeSDK(optional_error=optional_error)
    capture = sdk.capture(0, (2, 2))
    handle = sdk.handles[0]
    try:
        assert handle.calls == [
            ("speed", 3),
            (sdk.TOUPCAM_OPTION_FRAMERATE, 0),
            (sdk.TOUPCAM_OPTION_BYTEORDER, 1),
        ]
        handle.emit(2)
        handle.emit(7)
        ok, frame = capture.read(timeout=0)
        assert ok and np.all(frame == 7)
        handle.emit(9)
        assert np.all(frame == 7)  # No alias to SDK memory.
        assert np.all(capture.read(timeout=0)[1] == 9)
        assert capture.read(timeout=0) == (False, None)
    finally:
        capture.release()
        capture.release()
    assert handle.stopped == handle.closed == 1


def test_serial_selection_closes_other_devices_and_never_falls_back():
    sdk = FakeSDK()
    capture = sdk.capture(camera_serial="SERIAL-TWO")
    assert capture.device.id == "two"
    assert sdk.handles[0].closed == 1
    assert sdk.handles[1].closed == 0
    capture.release()
    sdk = FakeSDK()
    with pytest.raises(RuntimeError, match="unavailable"):
        sdk.capture(camera_serial="missing")
    assert all(handle.closed == 1 for handle in sdk.handles)
    assert all(not hasattr(handle, "callback") for handle in sdk.handles)


def test_settings_are_explicit_and_constructor_selection_overrides_environment(monkeypatch):
    monkeypatch.setenv("MG400_TOUPCAM_SERIAL", "SERIAL-TWO")
    assert selection() == (None, "SERIAL-TWO")
    assert selection(0) == (0, None)
    assert selection(settings={}) == (0, None)
    assert selection(settings={"MG400_TOUPCAM_INDEX": "1"}) == (1, None)
    camera = MicroscopeCamera(settings={"MG400_TOUPCAM_SERIAL": "SERIAL-TWO"})
    assert camera.camera_serial == "SERIAL-TWO" and camera.camera_index is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"camera_index": True},
        {"camera_index": -1},
        {"camera_index": "1"},
        {"camera_serial": " "},
        {"camera_serial": "非ASCII"},
        {"camera_index": 0, "camera_serial": "serial"},
        {"settings": {"MG400_TOUPCAM_INDEX": "nan"}},
        {"settings": {"MG400_TOUPCAM_INDEX": "0", "MG400_TOUPCAM_SERIAL": "serial"}},
    ],
)
def test_bad_selection_fails_before_sdk_enumeration(kwargs):
    sdk = FakeSDK()
    sdk.Toupcam.EnumV2 = Mock(side_effect=AssertionError("Unexpected SDK enumeration"))
    with pytest.raises(ValueError):
        ToupcamCapture(sdk=sdk, **kwargs)


def test_missing_library_error_describes_linux(monkeypatch):
    sdk = FakeSDK()
    sdk.Toupcam.EnumV2 = Mock(side_effect=OSError("library absent"))
    with pytest.raises(RuntimeError, match="libtoupcam.so.*LD_LIBRARY_PATH"):
        sdk.capture(0)


def test_capture_releases_handle_after_invalid_resolution():
    sdk = FakeSDK()
    with pytest.raises(ValueError, match="Unsupported resolution"):
        sdk.capture(0, (3, 3))
    assert sdk.handles[0].closed == 1


@pytest.mark.parametrize("event", [FakeSDK.TOUPCAM_EVENT_ERROR, FakeSDK.TOUPCAM_EVENT_DISCONNECTED])
def test_error_event_rejects_cached_frames_and_wakes_reader(event):
    sdk = FakeSDK()
    capture = sdk.capture(0)
    try:
        sdk.handles[0].emit(10)
        capture._handle_event(event)
        with pytest.raises(RuntimeError, match="acquisition failed"):
            capture.read(timeout=0)
    finally:
        capture.release()


def test_release_wakes_blocked_read_without_native_work_after_close():
    sdk = FakeSDK()
    capture = sdk.capture(0)
    replies = []
    worker = threading.Thread(target=lambda: replies.append(capture.read(timeout=5)))
    worker.start()
    capture.release()
    worker.join(1)
    assert not worker.is_alive() and replies == [(False, None)]
    sdk.handles[0].emit(8)
    assert capture.read(timeout=0) == (False, None)


def test_frame_packets_are_new_coherent_copies_and_reset_on_reopen():
    sdk = FakeSDK()
    camera = MicroscopeCamera(0, (2, 2), stabilization=False, capture_factory=sdk.capture)
    with camera:
        assert camera.get_frame_packet(wait=False) is None
        sdk.handles[-1].emit(3)
        first = camera.get_frame_packet()
        assert first[0] == 1 and isinstance(first[1], float)
        assert np.all(first[2] == 3)
        first[2][:] = 99
        assert np.all(camera.get_frame() == 3)
        assert camera.get_frame_packet(after_id=1, wait=False) is None
        sdk.handles[-1].emit(4)
        second = camera.get_frame_packet(after_id=1)
        assert second[0] == 2 and second[1] >= first[1]
        assert np.all(second[2] == 4)
        assert np.all(camera.get_frame(raw=True) == 4)
    assert not camera.is_open
    with camera:
        assert camera.get_frame_packet(wait=False) is None
        sdk.handles[-1].emit(6)
        assert camera.get_frame_packet()[0] == 1


def test_recording_interval_segments_and_metadata_are_preserved(tmp_path, monkeypatch):
    writers = []

    def writer(*args):
        result = Writer()
        writers.append(result)
        return result

    monkeypatch.setattr("toupcam_camera.camera.cv2.VideoWriter", writer)
    monkeypatch.setattr("toupcam_camera.camera.time.time", lambda: 100.0)
    session = _RecordingSession(
        tmp_path / "video.mp4",
        (2, 2),
        1,
        "mp4v",
        tmp_path / "times.csv",
        0.25,
        None,
        lambda: {"chip_id": "chip-1"},
        ("chip_id",),
        10,
    )
    frame = np.zeros((2, 2, 3), dtype=np.uint8)
    try:
        for timestamp in (100, 101, 109, 110, 119, 120):
            session.write(frame, timestamp)
        assert [len(item.frames) for item in writers] == [2, 1]
        assert [path.name for path in session.segments] == ["video_0001.mp4", "video_0002.mp4"]
    finally:
        session.close()
    assert all(item.released for item in writers)
    with (tmp_path / "times.csv").open() as source:
        rows = list(csv.DictReader(source))
    assert [float(row["unix_time"]) for row in rows] == [100, 110, 120]
    assert [row["segment_index"] for row in rows] == ["0", "0", "1"]
    assert all(row["chip_id"] == "chip-1" for row in rows)


def test_live_recording_and_disk_limit_stop_recording_without_stopping_feed(tmp_path, monkeypatch):
    writer = Writer()
    monkeypatch.setattr("toupcam_camera.camera.cv2.VideoWriter", lambda *args: writer)
    free = [10 * 1024**3]
    monkeypatch.setattr(
        "toupcam_camera.camera.shutil.disk_usage", lambda path: SimpleNamespace(free=free[0])
    )
    sdk = FakeSDK()
    with MicroscopeCamera(0, (2, 2), stabilization=False, capture_factory=sdk.capture) as camera:
        sdk.handles[-1].emit(1)
        camera.get_frame_packet()
        camera.start_recording(tmp_path / "live.mp4", mode="live", fps=7, segment_minutes=None)
        assert camera.recording_mode == "live"
        assert camera.recording_fps == 7 and camera.recording_interval_seconds is None
        free[0] = 0
        sdk.handles[-1].emit(2)
        wait_until(lambda: camera.recording_error is not None and writer.released)
        assert not camera.is_recording and camera.is_open
        assert "GB free" in str(camera.recording_error)
        sdk.handles[-1].emit(3)
        assert camera.get_frame_packet(after_id=2)[0] == 3


def test_duplicate_recording_request_does_not_truncate_active_metadata(tmp_path, monkeypatch):
    writers = []

    def writer(*args):
        result = Writer()
        writers.append(result)
        return result

    monkeypatch.setattr("toupcam_camera.camera.cv2.VideoWriter", writer)
    camera = MicroscopeCamera(settings={})
    camera._source = object()
    monkeypatch.setattr(camera, "get_frame", lambda **kwargs: np.zeros((2, 2, 3), dtype=np.uint8))
    path = tmp_path / "video.mp4"
    try:
        camera.start_recording(path, minimum_free_gb=None)
        camera._recording.write(np.zeros((2, 2, 3), dtype=np.uint8), time.time())
        original = path.with_suffix(".timestamps.csv").read_text()
        with pytest.raises(RuntimeError, match="already in progress"):
            camera.start_recording(path, minimum_free_gb=None)
        assert path.with_suffix(".timestamps.csv").read_text() == original
        assert len(writers) == 1
    finally:
        camera.stop_recording()


@pytest.mark.parametrize("event", [FakeSDK.TOUPCAM_EVENT_ERROR, FakeSDK.TOUPCAM_EVENT_DISCONNECTED])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_terminal_acquisition_failure_finalizes_active_recording(
    tmp_path, monkeypatch, event, cleanup_fails
):
    writer = Writer()
    monkeypatch.setattr("toupcam_camera.camera.cv2.VideoWriter", lambda *args: writer)
    sdk = FakeSDK()
    with MicroscopeCamera(0, (2, 2), stabilization=False, capture_factory=sdk.capture) as camera:
        handle = sdk.handles[-1]
        handle.emit(1)
        camera.get_frame_packet()
        camera.start_recording(tmp_path / "video.mp4", minimum_free_gb=None)
        session = camera._recording
        metadata = session._timestamp_file
        cleanup_error = OSError("recording cleanup failed")
        if cleanup_fails:
            close = session.close

            def failed_close():
                close()
                raise cleanup_error

            monkeypatch.setattr(session, "close", failed_close)
        handle.callback(event, handle.context)
        wait_until(lambda: camera.last_error is not None and not camera.is_recording)
        assert writer.released and metadata.closed
        assert camera.recording_error is (cleanup_error if cleanup_fails else camera.last_error)
        assert camera.recording_segments == session.segments
        with pytest.raises(RuntimeError, match="Camera acquisition stopped"):
            camera.get_frame_packet()
        assert camera.stop_recording() is None


def test_close_waits_for_recording_start_and_closes_its_writer(tmp_path, monkeypatch):
    writer = Writer()
    monkeypatch.setattr("toupcam_camera.camera.cv2.VideoWriter", lambda *args: writer)
    camera = MicroscopeCamera(settings={})
    camera._source = SimpleNamespace(release=lambda: None)
    entered, proceed, closed = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def frame(**kwargs):
        entered.set()
        assert proceed.wait(3)
        return np.zeros((2, 2, 3), dtype=np.uint8)

    def start():
        try:
            camera.start_recording(tmp_path / "video.mp4", minimum_free_gb=None)
        except BaseException as exc:
            errors.append(exc)

    def close():
        camera.close()
        closed.set()

    monkeypatch.setattr(camera, "get_frame", frame)
    starter = threading.Thread(target=start)
    closer = threading.Thread(target=close)
    starter.start()
    try:
        assert entered.wait(3)
        closer.start()
        assert not closed.wait(0.03)
    finally:
        proceed.set()
        starter.join(3)
        closer.join(3)
    assert not errors
    assert closed.is_set() and not camera.is_open and not camera.is_recording
    assert writer.released


def test_failed_recording_writer_is_released(tmp_path, monkeypatch):
    writer = Writer()
    monkeypatch.setattr(writer, "isOpened", lambda: False)
    monkeypatch.setattr("toupcam_camera.camera.cv2.VideoWriter", lambda *args: writer)
    camera = MicroscopeCamera(settings={})
    camera._source = object()
    monkeypatch.setattr(camera, "get_frame", lambda **kwargs: np.zeros((2, 2, 3), dtype=np.uint8))
    with pytest.raises(RuntimeError, match="Could not open video"):
        camera.start_recording(tmp_path / "video.mp4", minimum_free_gb=None)
    assert writer.released and not camera.is_recording


def test_stabilization_preserves_center_tracking_and_reset():
    import cv2

    camera = MicroscopeCamera(settings={}, smoothing=0)
    frame = np.random.default_rng(71).integers(0, 256, (128, 128, 3), dtype=np.uint8)
    shifted = cv2.warpAffine(frame, np.float32([[1, 0, 3], [0, 1, 2]]), (128, 128))
    assert np.array_equal(camera._stabilize(frame), frame)
    stabilized = camera._stabilize(shifted)
    np.testing.assert_allclose(camera._cumulative_motion, [3, 2], atol=0.1)
    reference = frame[8:-8, 8:-8].astype(float)
    assert np.mean(abs(stabilized[8:-8, 8:-8] - reference)) < np.mean(
        abs(shifted[8:-8, 8:-8] - reference)
    )
    camera.set_stabilization(False)
    assert camera._previous_gray is None
    np.testing.assert_array_equal(camera._cumulative_motion, [0, 0])
