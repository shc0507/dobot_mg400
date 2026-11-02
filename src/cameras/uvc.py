"""Shared UVC camera frames."""

from pathlib import Path
import json
import sys
import threading
import time

import cv2

from .usb import enumerate_usb_cameras


class USBCamera:
    """Threaded preview source for a standard USB/UVC camera.

    This is intentionally separate from :class:`MicroscopeCamera`: the main
    microscope camera uses the Toupcam SDK, while side-view webcams normally
    expose a Windows DirectShow/Media Foundation interface.
    """

    _BACKENDS = {
        "DSHOW": cv2.CAP_DSHOW,
        "MSMF": cv2.CAP_MSMF,
        "AUTO": cv2.CAP_ANY,
        "V4L2": cv2.CAP_V4L2,
    }

    def __init__(self, camera_index=0, resolution=(1280, 720), fps=30.0,
                 backend="AUTO", identity_path=None, preferred_index=3):
        self._auto_select = camera_index is None or str(camera_index).lower() == "auto"
        self.camera_index = None if self._auto_select else int(camera_index)
        self.identity_path = Path(identity_path) if identity_path else None
        self.preferred_index = preferred_index
        self.device_name = "USB camera"
        self.device_id = ""
        self.selection_warning = None
        self.resolution = resolution
        self.fps = float(fps)
        self.backend = str(backend).strip().upper() or "AUTO"
        if self.backend not in self._BACKENDS:
            raise ValueError(
                f"Unsupported USB-camera backend {backend!r}; use AUTO, "
                "DSHOW, or MSMF."
            )
        self._capture = None
        self._thread = None
        self._stop_event = threading.Event()
        self._frame_condition = threading.Condition()
        self._latest_frame = None
        self._frame_id = 0
        self._captured_at = None
        self._error = None
        self._frame_size = None
        self._active_backend = None

    @property
    def is_open(self):
        return self._capture is not None

    @property
    def frame_size(self):
        return self._frame_size

    @property
    def last_error(self):
        return self._error

    @property
    def description(self):
        return f"{self.device_name} (index {self.camera_index}, {self._active_backend})"

    def _select_auto_device(self):
        devices = enumerate_usb_cameras()
        candidates = [device for device in devices if device.is_ac410]
        if not candidates:
            found = ", ".join(f"{d.index}: {d.name}" for d in devices) or "none"
            raise RuntimeError(f"No Webcam AC410 found. Cameras detected: {found}")
        saved_id = ""
        if self.identity_path is not None:
            try:
                saved = json.loads(self.identity_path.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    saved_id = str(saved.get("device_id", ""))
            except (OSError, ValueError):
                pass
        if saved_id:
            known = [d for d in candidates if d.device_id.casefold() == saved_id.casefold()]
            if known:
                return known[0]
        if len(candidates) == 1:
            return candidates[0]
        # The operator previously identified Windows index 3 as the side view.
        # Use it only to bootstrap identity, not as a fallback when a saved
        # camera disappears while other same-model cameras remain attached.
        if not saved_id and sys.platform == "win32":
            preferred = [d for d in candidates if d.index == self.preferred_index]
            if preferred:
                return preferred[0]
        options = ", ".join(f"{d.index}: {d.name}" for d in candidates)
        raise RuntimeError(
            "Multiple AC410 cameras found, but the saved side camera could not "
            f"be identified. Reconnect the saved camera or select an explicit index. {options}"
        )

    def _remember_auto_device(self):
        if self.identity_path is None or not self.device_id:
            return
        temporary = self.identity_path.with_name(
            f"{self.identity_path.name}.{time.time_ns()}.tmp"
        )
        try:
            self.identity_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps({
                "device_id": self.device_id, "name": self.device_name,
                "last_index": self.camera_index, "backend": self._active_backend,
            }, indent=2), encoding="utf-8")
            temporary.replace(self.identity_path)
        except OSError as exc:
            self.selection_warning = f"Could not remember side camera: {exc}"
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def open(self):
        if self.is_open:
            return self
        self._stop_event.clear()
        self._error = None
        self._latest_frame = None
        self._frame_id = 0
        self._captured_at = None
        self.selection_warning = None
        source = self.camera_index
        if self._auto_select:
            device = self._select_auto_device()
            self.camera_index = device.index
            self.device_name = device.name
            self.device_id = device.device_id
            source = device.source
            # Device order differs between DirectShow and Media Foundation.
            # Keep the backend paired with the enumerated index/identity.
            backend_candidates = ((device.backend, self._BACKENDS[device.backend]),)
        elif self.backend == "AUTO" and sys.platform.startswith("linux"):
            backend_candidates = (("V4L2", cv2.CAP_V4L2),)
        elif self.backend == "AUTO":
            backend_candidates = (
                ("DSHOW", cv2.CAP_DSHOW),
                ("MSMF", cv2.CAP_MSMF),
                ("AUTO", cv2.CAP_ANY),
            )
        else:
            backend_candidates = ((self.backend, self._BACKENDS[self.backend]),)
        capture = None
        for backend_name, backend_value in backend_candidates:
            candidate = cv2.VideoCapture(source, backend_value)
            if candidate.isOpened():
                capture = candidate
                self._active_backend = backend_name
                break
            candidate.release()
        if capture is None:
            raise RuntimeError(
                f"Could not open USB camera index {self.camera_index} with "
                f"backend {self.backend}. Try another index or backend."
            )

        # MJPG lets many USB webcams deliver a larger preview without
        # saturating the USB bus. Unsupported properties are harmlessly
        # ignored by OpenCV/the camera driver.
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        if self.resolution is not None:
            width, height = self.resolution
            if width > 0:
                capture.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
            if height > 0:
                capture.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
        if self.fps > 0:
            capture.set(cv2.CAP_PROP_FPS, self.fps)

        self._capture = capture
        self._frame_size = (
            int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        )
        self._thread = threading.Thread(
            target=self._acquisition_loop,
            name=f"usb-camera-{self.camera_index}",
            daemon=True,
        )
        self._thread.start()
        if self._auto_select:
            try:
                if self.get_frame(wait=True, timeout=5.0) is None:
                    raise RuntimeError(f"{self.description} did not supply a frame")
                self._remember_auto_device()
            except BaseException:
                self.close()
                raise
        return self

    def _acquisition_loop(self):
        consecutive_failures = 0
        while not self._stop_event.is_set():
            capture = self._capture
            if capture is None:
                break
            try:
                ok, frame = capture.read()
            except Exception as exc:
                self._error = exc
                break
            if not ok or frame is None:
                consecutive_failures += 1
                if consecutive_failures >= 60:
                    self._error = RuntimeError(
                        "The USB camera opened but did not provide frames."
                    )
                    break
                time.sleep(0.03)
                continue
            consecutive_failures = 0
            self._frame_size = (frame.shape[1], frame.shape[0])
            with self._frame_condition:
                self._latest_frame = frame
                self._frame_id += 1
                self._captured_at = time.monotonic()
                self._frame_condition.notify_all()
        with self._frame_condition:
            self._frame_condition.notify_all()

    def get_frame(self, wait=True, timeout=2.0, copy=True):
        if not self.is_open:
            raise RuntimeError("USB camera is not open; call open() first")
        deadline = time.monotonic() + timeout
        with self._frame_condition:
            while self._latest_frame is None and wait and self._error is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._frame_condition.wait(remaining)
            if self._error is not None:
                raise RuntimeError("USB camera acquisition stopped") from self._error
            if self._latest_frame is None:
                return None
            return self._latest_frame.copy() if copy else self._latest_frame

    def close(self):
        self._stop_event.set()
        capture, self._capture = self._capture, None
        if capture is not None:
            capture.release()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        with self._frame_condition:
            self._latest_frame = None
            self._frame_condition.notify_all()
        self._active_backend = None

    def get_frame_packet(self, after_id=None, wait=True, timeout=2.0, copy=True):
        """Return (ID, monotonic receive time, frame) for a fresh USB frame."""
        deadline = time.monotonic() + timeout
        with self._frame_condition:
            while True:
                if not self.is_open:
                    raise RuntimeError("USB camera is closed")
                if self._error is not None:
                    raise RuntimeError("USB camera acquisition stopped") from self._error
                if (self._latest_frame is not None
                        and (after_id is None or self._frame_id > after_id)):
                    frame = self._latest_frame
                    return (
                        self._frame_id, self._captured_at,
                        frame.copy() if copy else frame,
                    )
                remaining = deadline - time.monotonic()
                if not wait or remaining <= 0:
                    return None
                self._frame_condition.wait(remaining)

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


