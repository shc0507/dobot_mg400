"""Enumerate UVC camera names and stable identities without opening streams."""

import fcntl
import os
import time
from dataclasses import dataclass
from pathlib import Path

USBDEVFS_RESET = ord("U") << 8 | 20


@dataclass(frozen=True)
class CameraDevice:
    index: int
    name: str
    device_id: str
    source: int | str
    backend: str

    @property
    def is_ac410(self):
        return "ac410" in self.name.casefold() or (
            "vid_f326" in self.device_id.casefold() and "pid_1306" in self.device_id.casefold()
        )


def enumerate_usb_cameras(
    *,
    sys_root=Path("/sys/class/video4linux"),
    by_path=Path("/dev/v4l/by-path"),
    by_id=Path("/dev/v4l/by-id"),
):
    """Read Linux metadata only. Stable hub paths distinguish identical cameras."""
    return _linux_devices(sys_root=sys_root, by_path=by_path, by_id=by_id)


def _linux_devices(*, sys_root, by_path, by_id):
    aliases = {str(path.resolve()): str(path) for path in sorted(by_id.glob("*"))}
    aliases.update({str(path.resolve()): str(path) for path in sorted(by_path.glob("*"))})
    devices = []
    for node in sorted(sys_root.glob("video*")):
        index = int(node.name.removeprefix("video"))
        source = f"/dev/{node.name}"
        try:
            name = (node / "name").read_text().strip()
        except OSError:
            continue
        identity = aliases.get(source, str(node.resolve()))
        devices.append(CameraDevice(index, name, identity, aliases.get(source, source), "V4L2"))
    return sorted(devices, key=lambda item: item.index)


def reset_usb_device(
    device, *, sys_root=Path("/sys/class/video4linux"), usb_root=Path("/dev/bus/usb"), wait_s=3.0
):
    """Reset the USB device of a camera, then wait until its stable name is back.

    The kernel binds the camera driver again, so the camera gets a new /dev/videoN node.
    A reader that has the camera open loses it.
    """
    usb = (sys_root / Path(device).resolve().name / "device").resolve().parent
    bus, number = (int((usb / name).read_text()) for name in ("busnum", "devnum"))
    handle = os.open(usb_root / f"{bus:03d}" / f"{number:03d}", os.O_WRONLY)
    try:
        fcntl.ioctl(handle, USBDEVFS_RESET, 0)
    finally:
        os.close(handle)
    deadline = time.monotonic() + wait_s
    while not Path(device).exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    # udev sets the group of the new node a moment after the name is back.
    time.sleep(0.2)
