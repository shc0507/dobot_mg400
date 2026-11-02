"""Enumerate UVC camera names and stable identities without opening streams."""

from dataclasses import dataclass
from pathlib import Path
import sys


@dataclass(frozen=True)
class CameraDevice:
    index: int
    name: str
    device_id: str
    source: int | str
    backend: str

    @property
    def is_ac410(self):
        return ("ac410" in self.name.casefold()
                or ("vid_f326" in self.device_id.casefold()
                    and "pid_1306" in self.device_id.casefold()))


def enumerate_usb_cameras():
    if sys.platform == "win32":
        return _windows_devices()
    if sys.platform.startswith("linux"):
        return _linux_devices()
    raise RuntimeError("Automatic camera selection supports Windows and Linux.")


def _linux_devices():
    aliases = {
        str(path.resolve()): str(path)
        for path in sorted(Path("/dev/v4l/by-id").glob("*"))
    }
    devices = []
    for node in sorted(Path("/sys/class/video4linux").glob("video*")):
        index = int(node.name.removeprefix("video"))
        source = f"/dev/{node.name}"
        try:
            name = (node / "name").read_text().strip()
        except OSError:
            continue
        identity = aliases.get(source, str(node.resolve()))
        devices.append(CameraDevice(index, name, identity, source, "V4L2"))
    return sorted(devices, key=lambda item: item.index)

