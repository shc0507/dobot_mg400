"""P1 acquisition adapter using the pinned p3-ir-camera USB protocol.

The adapter owns USB handles and must be used by one acquisition thread. It
adds Windows backend selection, deterministic cleanup and bounded frame
resynchronization without modifying the upstream implementation.
"""

from __future__ import annotations

import contextlib
import dataclasses
import struct
import sys
import time
from typing import Any

import libusb_package
import numpy as np
import usb.backend.libusb1
import usb.core
import usb.util

from ._vendor.p3_camera import (
    COMMANDS,
    CNT3_INCREMENT,
    CNT3_WRAP,
    MARKER_SIZE,
    GainMode,
    P3Camera,
    extract_both,
    get_model_config,
)

VID = 0x3474
PID = 0x45C2
DRIVER_HELP = (
    "On Windows, install WinUSB for the P1 (VID 3474, PID 45C2) with Zadig; "
    "see README.md. Close other camera applications and reconnect the USB cable."
)


class CameraError(RuntimeError):
    """Camera setup or streaming failure with an actionable message."""


class CameraTimeout(CameraError):
    """No complete, valid frame arrived within the acquisition deadline."""


def usb_backend():
    """Use the wheel's libusb library; no global DLL/PATH change is necessary."""
    backend = usb.backend.libusb1.get_backend(find_library=libusb_package.find_library)
    if backend is None:
        raise CameraError("libusb could not be loaded. Run `uv sync` to install libusb-package.")
    return backend


def diagnose() -> dict[str, Any]:
    """Inspect backend/device access without initializing or starting the camera."""
    result: dict[str, Any] = {
        "platform": sys.platform,
        "vid": "3474",
        "pid": "45C2",
        "backend_available": False,
        "device_found": False,
        "device_accessible": False,
        "hint": DRIVER_HELP,
    }
    device = None
    try:
        backend = usb_backend()
        result["backend_available"] = True
        device = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
        if device is None:
            result["error"] = "P1 not visible to libusb. Check cable, power and Windows driver."
            return result
        result["device_found"] = True
        result["bus"] = device.bus
        result["address"] = device.address
        config = device.get_active_configuration()
        result["interfaces"] = sorted({int(i.bInterfaceNumber) for i in config})
        result["device_accessible"] = True
        result["hint"] = "USB configuration is accessible. Run the capture test to verify streaming."
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if device is not None:
            with contextlib.suppress(Exception):
                usb.util.dispose_resources(device)
    return result


class FrameAssembler:
    """Recover full P1 frames even when transfer boundaries split markers.

    Validity requires marker lengths, sync bytes, parity and matching counters.
    Unaligned/partial shutter frames are discarded, never used for thermometry.
    """

    def __init__(self, frame_size: int):
        self.packet_size = frame_size + 2 * MARKER_SIZE
        self.buffer = bytearray()
        self.discarded_bytes = 0
        self.marker_mismatches = 0

    def clear(self) -> None:
        self.buffer.clear()

    def feed(self, chunk: bytes) -> None:
        self.buffer.extend(chunk)
        # A corrupt stream must not grow memory indefinitely.
        limit = self.packet_size * 3
        if len(self.buffer) > limit:
            remove = len(self.buffer) - limit
            del self.buffer[:remove]
            self.discarded_bytes += remove

    def pop(self) -> tuple[bytes, int, int] | None:
        while len(self.buffer) >= 2:
            even = self.buffer.find(b"\x0c\x8c")
            odd = self.buffer.find(b"\x0c\x8d")
            candidates = [position for position in (even, odd) if position >= 0]
            if not candidates:
                remove = len(self.buffer) - 1  # retain a possible split marker
                del self.buffer[:remove]
                self.discarded_bytes += remove
                return None
            start = min(candidates)
            if start:
                del self.buffer[:start]
                self.discarded_bytes += start
            if len(self.buffer) < self.packet_size:
                return None
            end = self.packet_size - MARKER_SIZE
            start_count = struct.unpack_from("<I", self.buffer, 2)[0]
            end_count = struct.unpack_from("<I", self.buffer, end + 2)[0]
            expected_sync = self.buffer[1] + 2
            if (
                self.buffer[end] != MARKER_SIZE
                or self.buffer[end + 1] != expected_sync
                or start_count != end_count
            ):
                self.marker_mismatches += 1
                del self.buffer[0]
                self.discarded_bytes += 1
                continue
            cnt3 = struct.unpack_from("<H", self.buffer, end + 10)[0]
            frame = bytes(self.buffer[:end])
            del self.buffer[: self.packet_size]
            return frame, start_count, cnt3
        return None


class _P1USB(P3Camera):
    def __init__(self, timeout: float):
        super().__init__(config=get_model_config("p1"))
        self.frame_timeout = timeout
        self.assembler = FrameAssembler(self.config.frame_size)
        self._claimed: list[int] = []
        self._detached: list[int] = []
        self._settling_frames = 0

    def connect(self) -> None:
        self.dev = usb.core.find(idVendor=VID, idProduct=PID, backend=usb_backend())
        if self.dev is None:
            raise CameraError(f"P1 camera not found (3474:45C2). {DRIVER_HELP}")
        try:
            # Windows WinUSB cannot set an already active configuration on some
            # composite devices, so only configure devices that need it.
            try:
                config = self.dev.get_active_configuration()
            except usb.core.USBError:
                self.dev.set_configuration()
                config = self.dev.get_active_configuration()
            interfaces = {int(i.bInterfaceNumber) for i in config}
            if not {0, 1}.issubset(interfaces):
                raise CameraError(f"P1 control/stream interfaces 0 and 1 missing: {interfaces}.")
            for interface in (0, 1):
                if sys.platform != "win32":
                    try:
                        if self.dev.is_kernel_driver_active(interface):
                            self.dev.detach_kernel_driver(interface)
                            self._detached.append(interface)
                    except NotImplementedError:
                        pass
                usb.util.claim_interface(self.dev, interface)
                self._claimed.append(interface)
        except Exception:
            self.disconnect()
            raise

    def start_streaming(self) -> None:
        self.assembler = FrameAssembler(self.config.frame_size)
        self._settling_frames = 0
        super().start_streaming()

    def disconnect(self) -> None:
        device = self.dev
        if device is None:
            return
        with contextlib.suppress(Exception):
            # Also undo a partially completed start_streaming().
            device.set_interface_altsetting(interface=1, alternate_setting=0)
        self.streaming = False
        for interface in reversed(self._claimed):
            with contextlib.suppress(Exception):
                usb.util.release_interface(device, interface)
        self._claimed.clear()
        for interface in reversed(self._detached):
            with contextlib.suppress(Exception):
                device.attach_kernel_driver(interface)
        self._detached.clear()
        with contextlib.suppress(Exception):
            usb.util.dispose_resources(device)
        self.dev = None
        self.assembler.clear()

    def read_frame(self) -> bytes:
        if self.dev is None or not self.streaming:
            raise CameraError("P1 is not streaming.")
        deadline = time.monotonic() + self.frame_timeout
        while time.monotonic() < deadline:
            complete = self.assembler.pop()
            if complete is not None:
                frame, count, cnt3 = complete
                self.stats.marker_mismatches = self.assembler.marker_mismatches
                if self._settling_frames:
                    self._settling_frames -= 1
                    continue
                if self.stats.frames_read:
                    expected = (self.stats.last_cnt3 + CNT3_INCREMENT) % CNT3_WRAP
                    gap = (cnt3 - expected) % CNT3_WRAP
                    if CNT3_INCREMENT // 2 < gap < CNT3_WRAP - CNT3_INCREMENT:
                        self.stats.frames_dropped += gap // CNT3_INCREMENT
                self.stats.frames_read += 1
                self.stats.last_cnt1, self.stats.last_cnt3 = count, cnt3
                return frame
            remaining_ms = max(1, min(250, int((deadline - time.monotonic()) * 1000)))
            try:
                chunk = self.dev.read(0x81, 16384, timeout=remaining_ms)
            except usb.core.USBTimeoutError:
                continue
            self.assembler.feed(bytes(chunk))
        raise CameraTimeout(f"No complete P1 frame received within {self.frame_timeout:g} seconds.")

    def trigger_shutter(self, return_partial: bool = False) -> None:
        # Upstream reconstructs a P3-sized split shutter frame. The P1 uses a
        # different shape: simply resynchronize to full validated frames.
        if not self.streaming:
            raise CameraError("P1 must be streaming to calibrate the shutter.")
        self._send_command(COMMANDS["shutter"])
        self._read_status()
        self.assembler.clear()
        self._settling_frames = 3


class P1Camera:
    """Reusable P1 driver. Own it from one thread or protect it with your lock."""

    def __init__(self, frame_timeout: float = 3.0):
        if not 0 < frame_timeout <= 30:
            raise ValueError("frame_timeout must be greater than 0 and at most 30 seconds")
        self._camera = _P1USB(frame_timeout)
        self._info: dict[str, Any] = {}

    def open(self) -> dict[str, Any]:
        if self._camera.dev is not None:
            return dict(self._info)
        try:
            self._camera.connect()
            name, firmware = self._camera.init()
            self._camera.set_gain_mode(GainMode.HIGH)
            self._camera.start_streaming()
            self._info = {
                # Registers contain C strings; bytes after the first NUL can
                # retain an earlier response, especially after reconnecting.
                "model": "P1", "name": name.split("\x00", 1)[0],
                "firmware": firmware.split("\x00", 1)[0],
                "vid": "3474", "pid": "45C2", "width": 160, "height": 120,
            }
            return dict(self._info)
        except Exception as exc:
            self.close()
            if isinstance(exc, CameraError):
                raise
            raise CameraError(f"Cannot open the P1: {exc}. {DRIVER_HELP}") from exc

    def read(self) -> tuple[np.ndarray, np.ndarray]:
        try:
            frame = self._camera.read_frame()
            ir, raw = extract_both(frame, self._camera.config)
            if ir is None or raw is None:
                raise CameraError("P1 returned an incomplete frame.")
            return ir, raw
        except usb.core.USBError as exc:
            raise CameraError(f"P1 USB read failed: {exc}. Reconnect the camera.") from exc

    def close(self) -> None:
        self._camera.disconnect()

    def shutter(self) -> None:
        self._camera.trigger_shutter()

    def set_gain(self, gain: str) -> None:
        if gain == "low":
            raise ValueError("Low gain is disabled: temperature conversion has not been validated on the P1")
        if gain != "high":
            raise ValueError("gain must be 'high'")
        self._camera.set_gain_mode(GainMode.HIGH)
        self._camera.assembler.clear()
        self._camera._settling_frames = 3

    def stats(self) -> dict[str, Any]:
        return {
            **dataclasses.asdict(self._camera.stats),
            "discarded_bytes": self._camera.assembler.discarded_bytes,
            "gain": self._camera.gain_mode.name.lower(),
        }

    def __enter__(self) -> P1Camera:
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
