"""Shared, standard-library-only AC410 stream settings; no device or file access."""

import re
from dataclasses import dataclass

MJPEG_MODES = frozenset(
    {
        (1920, 1080, 60),
        (1920, 1080, 30),
        (4000, 3000, 15),
        (3840, 2160, 30),
        (2592, 1944, 30),
        (2560, 1440, 30),
        (2048, 1536, 30),
        (1600, 1200, 30),
        (1280, 720, 30),
        (1024, 768, 30),
        (1024, 576, 30),
        (800, 600, 30),
        (640, 480, 30),
        (640, 360, 30),
        (320, 240, 30),
    }
)
# A by-path name follows the USB port. A by-id name follows the serial number: only the
# wrist camera has a serial of its own, so only that camera uses one.
DEVICE = re.compile(r"/dev/v4l/by-(?:path|id)/[A-Za-z0-9_.:+-]{1,255}")
# The largest MJPEG mode, for a full-resolution still: AC410, then the wrist camera.
STILL_MODE = (4000, 3000, 15)
WRIST_STILL_MODE = (3840, 2160, 30)
MODE = re.compile(r"([1-9][0-9]{0,3})x([1-9][0-9]{0,3})@([1-9][0-9]{0,2})")


def _text(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string.")
    return value.strip()


def camera_devices(value):
    """Return 1–5 distinct stable device paths, or an empty list for cameras off."""
    value = _text(value, "MG400_CAMERA_DEVICES")
    if not value:
        return []
    devices = [part.strip() for part in value.split(",")]
    if not 1 <= len(devices) <= 5:
        raise ValueError("MG400_CAMERA_DEVICES must list 1 to 5 devices or none.")
    for device in devices:
        if not DEVICE.fullmatch(device) or device.rsplit("/", 1)[-1] in {".", ".."}:
            raise ValueError(f"Invalid camera device path: {device!r}")
    if len(set(devices)) != len(devices):
        raise ValueError("MG400_CAMERA_DEVICES must not repeat a device.")
    return devices


@dataclass(frozen=True)
class CameraConfig:
    devices: tuple[str, ...]
    modes: tuple[tuple[int, int, int], ...]

    @property
    def count(self):
        return len(self.devices)

    @property
    def streams(self):
        return tuple(f"camera{number}" for number in range(1, self.count + 1))

    @property
    def preloaded(self):
        return tuple(
            stream
            for stream, (width, height, _) in zip(self.streams, self.modes)
            if width <= 1280 and height <= 720
        )

    @property
    def wrist(self):
        """The devices with a by-id name: the wrist camera."""
        return tuple(device for device in self.devices if device.startswith("/dev/v4l/by-id/"))

    @property
    def held(self):
        """Streams that go2rtc holds open: each preloaded stream and the wrist camera.

        The wrist camera can refuse an open after a stream stop, so its stream does not stop.
        """
        return tuple(
            stream
            for stream, device in zip(self.streams, self.devices)
            if stream in self.preloaded or device in self.wrist
        )

    def large(self, side=""):
        """Streams above 1280x720 that a viewer starts. The microscope side stream is not one."""
        return tuple(
            stream for stream in self.streams if stream not in self.preloaded and stream != side
        )

    def mode(self, camera):
        if type(camera) is not int or not 1 <= camera <= self.count:
            raise ValueError("Camera must identify a configured stream.")
        return self.modes[camera - 1]

    def still_mode(self, camera):
        """The mode of a full-resolution still. A camera above 1280x720 keeps its own mode."""
        mode = self.mode(camera)
        if self.streams[camera - 1] not in self.preloaded:
            return mode
        return WRIST_STILL_MODE if self.devices[camera - 1] in self.wrist else STILL_MODE


def camera_configuration(settings):
    """Resolve the authoritative mode list or the compatible legacy size settings."""
    devices = tuple(camera_devices(settings.get("MG400_CAMERA_DEVICES", "")))
    modes = _text(settings.get("MG400_CAMERA_MODES", ""), "MG400_CAMERA_MODES")
    sizes = _text(settings.get("MG400_CAMERA_CAPTURE_SIZES", ""), "MG400_CAMERA_CAPTURE_SIZES")
    side = _text(settings.get("MG400_MICROSCOPE_SIDE_STREAM", ""), "MG400_MICROSCOPE_SIDE_STREAM")
    if modes and sizes:
        raise ValueError("MG400_CAMERA_MODES and MG400_CAMERA_CAPTURE_SIZES cannot both be set.")
    if not devices:
        if modes or sizes:
            raise ValueError("Camera modes and capture sizes require configured camera devices.")
        return CameraConfig((), ())
    streams = tuple(f"camera{number}" for number in range(1, len(devices) + 1))
    if side and side not in streams:
        raise ValueError("MG400_MICROSCOPE_SIDE_STREAM must name a configured camera stream.")
    if not modes and not sizes:
        return CameraConfig(
            devices,
            tuple((3840, 2160, 30) if stream == side else (1280, 720, 30) for stream in streams),
        )
    parts = [part.strip() for part in (modes or sizes).split(",")]
    name = "MG400_CAMERA_MODES" if modes else "MG400_CAMERA_CAPTURE_SIZES"
    if len(parts) != len(devices):
        raise ValueError(f"{name} must give one mode or size per camera device.")
    parsed = []
    for part in parts:
        text = part if modes else part + "@30"
        match = MODE.fullmatch(text)
        mode = tuple(map(int, match.groups())) if match is not None else None
        if mode not in MJPEG_MODES:
            raise ValueError(f"Unsupported AC410 MJPEG mode in {name}: {part!r}.")
        parsed.append(mode)
    return CameraConfig(devices, tuple(parsed))
