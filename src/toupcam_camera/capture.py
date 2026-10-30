"""OpenCV-compatible frame source for the Toupcam SDK."""

import ctypes
import importlib
import os
import queue
import threading

import numpy as np


def selection(camera_index=None, camera_serial=None, *, settings=None):
    """Use explicit selection, or MG400_TOUPCAM_INDEX / MG400_TOUPCAM_SERIAL.

    The default index is 0. A serial never falls back to an index. Set only one.
    """
    if camera_index is None and camera_serial is None:
        settings = os.environ if settings is None else settings
        camera_serial = settings.get("MG400_TOUPCAM_SERIAL") or None
        raw_index = settings.get("MG400_TOUPCAM_INDEX") or None
        if raw_index is not None:
            if (
                not isinstance(raw_index, str)
                or not raw_index.isascii()
                or not raw_index.isdecimal()
            ):
                raise ValueError("MG400_TOUPCAM_INDEX must be a nonnegative integer.")
            camera_index = int(raw_index)
    if camera_index is not None and camera_serial is not None:
        raise ValueError("Select a camera by index or serial number, not both.")
    if camera_serial is not None:
        if (
            not isinstance(camera_serial, str)
            or not camera_serial
            or not camera_serial.isascii()
            or any(not "!" <= char <= "~" for char in camera_serial)
        ):
            raise ValueError("Camera serial must contain non-space printable ASCII characters.")
        return None, camera_serial
    if camera_index is None:
        camera_index = 0
    if type(camera_index) is not int or camera_index < 0:
        raise ValueError("Camera index must be a nonnegative integer.")
    return camera_index, None


class ToupcamCapture:
    def __init__(
        self, camera_index=None, resolution=None, *, camera_serial=None, settings=None, sdk=None
    ):
        camera_index, camera_serial = selection(camera_index, camera_serial, settings=settings)
        self._sdk = sdk if sdk is not None else importlib.import_module(".toupcam", __package__)
        try:
            devices = self._sdk.Toupcam.EnumV2()
        except OSError as exc:
            raise RuntimeError(
                "Cannot load libtoupcam.so. Install the approved Linux SDK library "
                "and set LD_LIBRARY_PATH."
            ) from exc
        if not devices:
            raise RuntimeError("No Toupcam camera found. Check its USB connection and permissions.")
        self.camera = None
        if camera_serial is not None:
            for device in devices:
                candidate = self._sdk.Toupcam.Open(device.id)
                if candidate is None:
                    continue
                try:
                    serial = candidate.SerialNumber()
                    if isinstance(serial, bytes):
                        serial = serial.decode("ascii")
                    if serial == camera_serial:
                        self.device, self.camera = device, candidate
                        break
                finally:
                    if candidate is not self.camera:
                        candidate.Close()
            if self.camera is None:
                raise RuntimeError(
                    f"Camera serial {camera_serial!r} is unavailable. No index fallback was used."
                )
        else:
            if camera_index >= len(devices):
                raise ValueError(
                    f"Invalid camera index {camera_index}; found {len(devices)} cameras."
                )
            self.device = devices[camera_index]
            self.camera = self._sdk.Toupcam.Open(self.device.id)
            if self.camera is None:
                raise RuntimeError(f"Could not open camera: {self.device.displayname}")
        self._frames = queue.Queue(maxsize=1)
        self._callback_lock = threading.RLock()
        self._error = None
        self._closed = False
        try:
            modes = [(r.width, r.height) for r in self.device.model.res]
            if resolution is not None:
                if resolution not in modes:
                    raise ValueError(f"Unsupported resolution {resolution}; available: {modes}")
                self.camera.put_eSize(modes.index(resolution))
            self.width, self.height = self.camera.get_Size()
            # Use the fastest sensor/readout mode supported by this camera and
            # remove the SDK's optional software frame-rate limit.  The actual
            # frame rate can still be bounded by resolution and exposure time.
            try:
                self.camera.put_Speed(int(self.device.model.maxspeed))
            except Exception:
                pass
            try:
                self.camera.put_Option(self._sdk.TOUPCAM_OPTION_FRAMERATE, 0)
            except Exception:
                pass
            self.camera.put_Option(self._sdk.TOUPCAM_OPTION_BYTEORDER, 1)
            self._buffer = ctypes.create_string_buffer(self.width * self.height * 3)
            self.camera.StartPullModeWithCallback(self._event_callback, self)
        except Exception:
            self.camera.Close()
            self.camera = None
            raise

    @staticmethod
    def _event_callback(event, context):
        context._handle_event(event)

    def _publish(self, frame):
        try:
            self._frames.get_nowait()
        except queue.Empty:
            pass
        self._frames.put_nowait(frame)

    def _handle_event(self, event):
        with self._callback_lock:
            if self._closed:
                return
            if event == self._sdk.TOUPCAM_EVENT_IMAGE:
                try:
                    self.camera.PullImageV4(self._buffer, 0, 24, -1, None)
                    frame = (
                        np.frombuffer(self._buffer, np.uint8)
                        .reshape(self.height, self.width, 3)
                        .copy()
                    )
                    self._publish(frame)
                except Exception as exc:
                    self._error = exc
                    self._publish(None)
            elif event in (self._sdk.TOUPCAM_EVENT_ERROR, self._sdk.TOUPCAM_EVENT_DISCONNECTED):
                self._error = RuntimeError(f"Toupcam event 0x{event:x}")
                self._publish(None)

    def read(self, timeout=2.0):
        if self._closed:
            return False, None
        if self._error is not None:
            raise RuntimeError("Toupcam acquisition failed") from self._error
        try:
            frame = self._frames.get(timeout=timeout)
        except queue.Empty:
            return False, None
        if self._error is not None:
            raise RuntimeError("Toupcam acquisition failed") from self._error
        return (False, None) if self._closed or frame is None else (True, frame)

    def release(self):
        with self._callback_lock:
            if self._closed:
                return
            self._closed = True
            self._publish(None)
        # Stop may wait for a callback. Do not hold the callback lock here.
        if self.camera is not None:
            try:
                self.camera.Stop()
            finally:
                self.camera.Close()
                self.camera = None

    @property
    def sdk_camera(self):
        return self.camera
