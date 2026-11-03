import csv
import ctypes
import hashlib
import os
import stat
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


@pytest.fixture
def private_run(tmp_path):
    previous = os.umask(0o022)
    try:
        run = tmp_path / "run"
        run.mkdir(mode=0o750)
        yield run
    finally:
        os.umask(previous)


def private_session(run, **kwargs):
    return _RecordingSession(
        run / "video.mp4",
        (2, 2),
        1,
        "mp4v",
        run / "video.timestamps.csv",
        kwargs.pop("segment_minutes", 0.25),
        None,
        lambda: {"chip_id": "chip-1"},
        ("chip_id",),
        private_output=True,
        **kwargs,
    )


def fake_recording_camera(monkeypatch):
    camera = MicroscopeCamera(settings={})
    camera._source = object()
    monkeypatch.setattr(camera, "get_frame", lambda **kwargs: np.zeros((2, 2, 3), dtype=np.uint8))
    return camera


def test_private_recording_reserves_initial_and_rollover_at_birth(private_run, monkeypatch):
    paths, writers = [], []

    def writer(path, *args):
        assert path.startswith("/proc/self/fd/") and path.endswith(".mp4")
        info = os.stat(path)
        assert stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
        assert stat.S_IMODE((private_run / "video.timestamps.csv").stat().st_mode) == 0o600
        Path(path).write_bytes(b"fake video")
        assert os.stat(path).st_ino == info.st_ino
        result = Writer()
        paths.append(path)
        writers.append(result)
        return result

    monkeypatch.setattr("toupcam_camera.camera.cv2.VideoWriter", writer)
    monkeypatch.setattr("toupcam_camera.camera.time.time", lambda: 100.0)
    camera = fake_recording_camera(monkeypatch)
    try:
        assert (
            camera.start_recording(
                private_run / "video.mp4",
                mode="live",
                segment_minutes=0.25,
                minimum_free_gb=None,
                metadata_fields=("chip_id",),
                metadata_provider=lambda: {"chip_id": "chip-1"},
                private_output=True,
            )
            == private_run / "video.mp4"
        )
        session = camera._recording
        parent_fd = session._private_outputs.descriptor
        for captured_at in (100, 110, 120):
            session.write(np.zeros((2, 2, 3), dtype=np.uint8), captured_at)
        assert [len(item.frames) for item in writers] == [2, 1]
        assert [path.name for path in camera.recording_segments] == [
            "video_0001.mp4",
            "video_0002.mp4",
        ]
        assert all(f"/proc/self/fd/{parent_fd}/" in path for path in paths)
    finally:
        camera.stop_recording()
    with pytest.raises(OSError):
        os.fstat(parent_fd)
    assert all(item.released for item in writers)
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in private_run.iterdir())
    with (private_run / "video.timestamps.csv").open() as source:
        rows = list(csv.DictReader(source))
    assert [row["unix_time"] for row in rows] == ["100", "110", "120"]
    assert [row["segment_index"] for row in rows] == ["0", "0", "1"]
    assert all(row["chip_id"] == "chip-1" for row in rows)


@pytest.mark.parametrize("filename", ["video.timestamps.csv", "video_0001.mp4"])
@pytest.mark.parametrize("kind", ["file", "symlink", "hardlink"])
def test_private_recording_collision_never_overwrites_existing(
    private_run, monkeypatch, filename, kind
):
    existing = private_run.parent / "existing"
    existing.write_bytes(b"retain existing")
    path = private_run / filename
    if kind == "symlink":
        path.symlink_to(existing)
    elif kind == "hardlink":
        os.link(existing, path)
    else:
        path.write_bytes(b"retain existing")
    writer = Mock(side_effect=AssertionError("A collision must precede native writer admission"))
    monkeypatch.setattr("toupcam_camera.camera.cv2.VideoWriter", writer)
    with pytest.raises(FileExistsError):
        private_session(private_run)
    assert path.read_bytes() == existing.read_bytes() == b"retain existing"
    writer.assert_not_called()


def test_private_rollover_collision_preserves_previous_segment(private_run, monkeypatch):
    writer = Writer()
    monkeypatch.setattr("toupcam_camera.camera.cv2.VideoWriter", lambda *args: writer)
    monkeypatch.setattr("toupcam_camera.camera.time.time", lambda: 100.0)
    session = private_session(private_run)
    first = private_run / "video_0001.mp4"
    first.write_bytes(b"completed local frames")
    collision = private_run / "video_0002.mp4"
    collision.write_bytes(b"earlier recording")
    try:
        with pytest.raises(FileExistsError):
            session.write(np.zeros((2, 2, 3), dtype=np.uint8), 120)
    finally:
        session.close()
    assert writer.released and first.read_bytes() == b"completed local frames"
    assert collision.read_bytes() == b"earlier recording"
    assert session.segments == [first]


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o777])
def test_private_recording_rejects_public_or_writable_parent(private_run, monkeypatch, mode):
    private_run.chmod(mode)
    writer = Mock(side_effect=AssertionError("Unsafe parent must precede native writer"))
    monkeypatch.setattr("toupcam_camera.camera.cv2.VideoWriter", writer)
    with pytest.raises(ValueError, match="owned and private"):
        private_session(private_run)
    assert not list(private_run.iterdir())
    writer.assert_not_called()


@pytest.mark.parametrize("kind", ["relative", "traversal", "symlink", "ancestor", "missing"])
def test_private_recording_rejects_unsafe_paths_before_creation(private_run, monkeypatch, kind):
    if kind == "relative":
        path = Path("relative-recording")
    elif kind == "traversal":
        path = private_run / ".." / "run"
    elif kind == "symlink":
        path = private_run.parent / "link"
        path.symlink_to(private_run, target_is_directory=True)
    elif kind == "ancestor":
        alias = private_run.parent / "alias"
        alias.symlink_to(private_run.parent, target_is_directory=True)
        path = alias / "run"
    else:
        path = private_run / "missing"
    writer = Mock(side_effect=AssertionError("Unsafe paths must precede native writer"))
    monkeypatch.setattr("toupcam_camera.camera.cv2.VideoWriter", writer)
    with pytest.raises((ValueError, OSError)):
        private_session(path)
    assert not list(private_run.iterdir())
    writer.assert_not_called()


def test_private_recording_requires_same_owned_existing_parent(private_run, monkeypatch):
    camera = fake_recording_camera(monkeypatch)
    with pytest.raises(ValueError, match="one directory"):
        camera.start_recording(
            private_run / "video.mp4",
            timestamps_path=private_run.parent / "timestamps.csv",
            private_output=True,
        )
    identity = os.geteuid()
    monkeypatch.setattr("toupcam_camera.camera.os.geteuid", lambda: identity + 1)
    with pytest.raises(ValueError, match="owned and private"):
        private_session(private_run)
    assert not list(private_run.iterdir())


@pytest.mark.parametrize("swap_at", ["open", "close"])
def test_private_writer_pins_parent_and_rejects_ancestor_swap(private_run, monkeypatch, swap_at):
    replacement = private_run.parent / "replacement"
    replacement.mkdir(mode=0o750)
    (replacement / "video_0001.mp4").write_bytes(b"outside original")
    original = private_run.parent / "original-run"
    writer = Writer()
    descriptors = []

    def swap():
        private_run.rename(original)
        private_run.symlink_to(replacement, target_is_directory=True)

    def native(path, *args):
        descriptors.append(int(path.split("/")[-2]))
        if swap_at == "open":
            swap()
        Path(path).write_bytes(b"private original frames")
        return writer

    monkeypatch.setattr("toupcam_camera.camera.cv2.VideoWriter", native)
    if swap_at == "open":
        with pytest.raises((ValueError, OSError)):
            private_session(private_run)
    else:
        session = private_session(private_run)
        swap()
        with pytest.raises((ValueError, OSError)):
            session.close()
        session.close()
    assert (replacement / "video_0001.mp4").read_bytes() == b"outside original"
    assert sorted(path.name for path in replacement.iterdir()) == ["video_0001.mp4"]
    assert (original / "video_0001.mp4").read_bytes() == b"private original frames"
    assert writer.released
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def test_private_failure_cleanup_does_not_unlink_replaced_reservation(private_run, monkeypatch):
    writer = Writer()

    def replace(path, *args):
        Path(path).unlink()
        Path(path).write_bytes(b"")
        return writer

    monkeypatch.setattr("toupcam_camera.camera.cv2.VideoWriter", replace)
    with pytest.raises(ValueError, match="changed or is unsafe"):
        private_session(private_run)
    assert (private_run / "video_0001.mp4").exists()
    assert writer.released


@pytest.mark.parametrize("failure", ["construct", "open", "release"])
def test_private_failed_writer_closes_owned_resources(private_run, monkeypatch, failure):
    from toupcam_camera.camera import _PrivateRecordingOutputs

    reserved, parents = [], []
    reserve = _PrivateRecordingOutputs.reserve

    def track(self, name):
        parents.append(self.descriptor)
        descriptor = reserve(self, name)
        reserved.append(descriptor)
        return descriptor

    monkeypatch.setattr(_PrivateRecordingOutputs, "reserve", track)
    writer = Writer()
    monkeypatch.setattr(writer, "isOpened", lambda: False)

    def native(*args):
        if failure == "construct":
            raise RuntimeError("constructor failed")
        return writer

    def failed_release():
        writer.released = True
        raise RuntimeError("release failed")

    if failure == "release":
        monkeypatch.setattr(writer, "release", failed_release)
    monkeypatch.setattr("toupcam_camera.camera.cv2.VideoWriter", native)
    with pytest.raises(RuntimeError):
        private_session(private_run)
    assert not (private_run / "video_0001.mp4").exists()
    assert (private_run / "video.timestamps.csv").read_text().startswith("frame_index,")
    assert failure == "construct" or writer.released
    for descriptor in set(reserved + parents):
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize("boundary", ["before", "lifecycle", "frame", "recording"])
def test_cancelled_recording_never_allocates_after_admission_wait(
    private_run, monkeypatch, boundary
):
    camera = fake_recording_camera(monkeypatch)
    cancel = threading.Event()
    reached, proceed = threading.Event(), threading.Event()
    errors = []
    writer = Mock(side_effect=AssertionError("Cancelled recording must not open writer"))
    monkeypatch.setattr("toupcam_camera.camera.cv2.VideoWriter", writer)

    def frame(**kwargs):
        reached.set()
        if boundary == "frame":
            assert proceed.wait(3)
        return np.zeros((2, 2, 3), dtype=np.uint8)

    monkeypatch.setattr(camera, "get_frame", frame)

    def record():
        try:
            if boundary == "lifecycle":
                reached.set()
            camera.start_recording(private_run / "video.mp4", private_output=True, cancel=cancel)
        except BaseException as error:
            errors.append(error)

    lock = (
        camera._lifecycle_lock
        if boundary == "lifecycle"
        else camera._recording_lock
        if boundary == "recording"
        else None
    )
    if lock is not None:
        lock.acquire()
    if boundary == "before":
        cancel.set()
    worker = threading.Thread(target=record)
    try:
        worker.start()
        if boundary != "before":
            assert reached.wait(3)
            cancel.set()
    finally:
        proceed.set()
        if lock is not None:
            lock.release()
        worker.join(3)
    assert not worker.is_alive() and len(errors) == 1
    assert isinstance(errors[0], RuntimeError) and "cancelled" in str(errors[0])
    assert not list(private_run.iterdir()) and not camera.is_recording
    writer.assert_not_called()


def test_cancelled_stabilization_wait_cannot_mutate_camera(monkeypatch):
    camera = fake_recording_camera(monkeypatch)
    camera.stabilization_enabled = False
    marker = object()
    camera._previous_gray = marker
    cancel, reached = threading.Event(), threading.Event()
    errors = []

    def stabilize():
        reached.set()
        try:
            camera.set_stabilization(True, cancel=cancel)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=stabilize)
    with camera._stabilization_lock:
        worker.start()
        assert reached.wait(3)
        cancel.set()
    worker.join(3)
    assert not worker.is_alive() and len(errors) == 1 and "cancelled" in str(errors[0])
    assert not camera.stabilization_enabled and camera._previous_gray is marker


def test_admitted_recording_may_drain_without_holding_cancellation_lock(private_run, monkeypatch):
    camera = fake_recording_camera(monkeypatch)
    cancel = threading.Event()
    reached, proceed = threading.Event(), threading.Event()
    errors = []
    writer = Writer()

    def native(*args):
        reached.set()
        assert proceed.wait(3)
        return writer

    monkeypatch.setattr("toupcam_camera.camera.cv2.VideoWriter", native)

    def record():
        try:
            camera.start_recording(private_run / "video.mp4", private_output=True, cancel=cancel)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=record)
    worker.start()
    try:
        assert reached.wait(3)
        cancel.set()
    finally:
        proceed.set()
        worker.join(3)
        camera.stop_recording()
    assert not worker.is_alive() and not errors and writer.released


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
