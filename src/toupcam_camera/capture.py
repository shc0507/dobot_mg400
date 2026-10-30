"""OpenCV-compatible frame source for the Toupcam SDK."""
from pathlib import Path
import queue
import sys
import numpy as np

sdk_directory = Path(__file__).resolve().parent / "python"
if str(sdk_directory) not in sys.path:
    sys.path.insert(0, str(sdk_directory))
import python.toupcam as toupcam

class ToupcamCapture:
    def __init__(self, camera_index=0, resolution=None):
        try:
            devices = toupcam.Toupcam.EnumV2()
        except OSError as exc:
            raise RuntimeError("Could not load toupcam.dll. Copy the 64-bit SDK DLL into the project's 'python' folder.") from exc
        if not devices:
            raise RuntimeError("No Toupcam camera found. Check its USB connection and driver.")
        if not 0 <= camera_index < len(devices):
            raise ValueError(f"Invalid camera index {camera_index}; found {len(devices)} camera(s).")
        self.device = devices[camera_index]
        self.camera = toupcam.Toupcam.Open(self.device.id)
        if self.camera is None:
            raise RuntimeError(f"Could not open camera: {self.device.displayname}")
        self._frames = queue.Queue(maxsize=1)
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
                self.camera.put_Option(toupcam.TOUPCAM_OPTION_FRAMERATE, 0)
            except Exception:
                pass
            self.camera.put_Option(toupcam.TOUPCAM_OPTION_BYTEORDER, 1)
            self._buffer = bytes(self.width * self.height * 3)
            self.camera.StartPullModeWithCallback(self._event_callback, self)
        except Exception:
            self.camera.Close()
            self.camera = None
            raise
        print(f"Opened {self.device.displayname}: {self.width}x{self.height}")

    @staticmethod
    def _event_callback(event, context):
        context._handle_event(event)

    def _handle_event(self, event):
        if event == toupcam.TOUPCAM_EVENT_IMAGE:
            try:
                self.camera.PullImageV4(self._buffer, 0, 24, -1, None)
                frame = np.frombuffer(self._buffer, np.uint8).reshape(self.height, self.width, 3).copy()
                try:
                    self._frames.get_nowait()
                except queue.Empty:
                    pass
                self._frames.put_nowait(frame)
            except Exception as exc:
                self._error = exc
        elif event in (toupcam.TOUPCAM_EVENT_ERROR, toupcam.TOUPCAM_EVENT_DISCONNECTED):
            self._error = RuntimeError(f"Toupcam event 0x{event:x}")

    def read(self, timeout=2.0):
        if self._closed:
            return False, None
        if self._error is not None:
            raise RuntimeError("Toupcam acquisition failed") from self._error
        try:
            return True, self._frames.get(timeout=timeout)
        except queue.Empty:
            return False, None

    def release(self):
        if not self._closed:
            self._closed = True
            if self.camera is not None:
                try:
                    self.camera.Stop()
                finally:
                    self.camera.Close()
                    self.camera = None

    @property
    def sdk_camera(self):
        return self.camera
