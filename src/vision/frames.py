"""Bounded frame reads from the local go2rtc service or offline demo fixtures."""

from __future__ import annotations

import math
import os
import time
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

from cameras import go2rtc
from cameras.config import camera_configuration
from cameras.go2rtc import FrameError

__all__ = ["DemoSource", "FrameError", "Go2RTCSource", "still", "validate_camera"]

_MAX_BYTES = 8 * 1024 * 1024
_MAX_PIXELS = 16_000_000


def validate_camera(camera: int, count: int = 4) -> int:
    """Return a configured camera number; reject other types and values."""
    if type(count) is not int or not 0 <= count <= 5:
        raise ValueError("Configured camera count must be an integer from 0 through 5.")
    if type(camera) is not int or not 1 <= camera <= count:
        raise ValueError("camera must identify a configured stream from camera1 to camera5.")
    return camera


def _decode(data: bytes, image_format: str) -> Image.Image:
    """Check the format and dimensions before loading the image pixels."""
    try:
        with Image.open(BytesIO(data), formats=(image_format,)) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > _MAX_PIXELS:
                raise FrameError(f"Frame exceeds the {_MAX_PIXELS}-pixel limit.")
            image.verify()
        # verify() invalidates the decoder; reopen the same bounded bytes.
        with Image.open(BytesIO(data), formats=(image_format,)) as image:
            image.load()
            return ImageOps.exif_transpose(image).convert("RGB")
    except FrameError:
        raise
    except Exception as exc:
        raise FrameError(f"Cannot decode {image_format} frame: {exc}") from exc


class Go2RTCSource:
    """Read JPEG frames from go2rtc, which remains the only camera owner.

    The optional opener follows ``cameras.go2rtc.read_jpeg``. Test openers must
    not perform network access. Construction opens no network connection.
    """

    def __init__(
        self,
        *,
        timeout: float = 2.0,
        max_bytes: int = _MAX_BYTES,
        opener=None,
        clock=time.monotonic,
        settings=None,
    ):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a positive finite number.")
        try:
            timeout = float(timeout)
        except OverflowError as exc:
            raise ValueError("timeout must be a positive finite number.") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number.")
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer.")
        if (
            opener is not None
            and not callable(opener)
            and not callable(getattr(opener, "open", None))
        ):
            raise ValueError("opener must be callable or provide an open method.")
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._opener = opener
        self._clock = clock
        self.configuration = camera_configuration(os.environ if settings is None else settings)
        self.camera_count = self.configuration.count

    def read(self, camera: int) -> Image.Image:
        """Return an EXIF-oriented RGB frame, or raise FrameError on failure."""
        return self.still(camera)

    def still(self, camera: int) -> Image.Image:
        """Read one frame from the same configured stream and check its dimensions."""
        camera = validate_camera(camera, self.camera_count)
        try:
            data = go2rtc.read_jpeg(
                f"camera{camera}",
                timeout=self.timeout,
                max_bytes=self.max_bytes,
                opener=self._opener,
                clock=self._clock,
            )
            image = _decode(data, "JPEG")
            expected = self.configuration.mode(camera)[:2]
            if image.size != expected:
                raise FrameError(
                    f"Camera {camera} frame size {image.size[0]}x{image.size[1]} "
                    f"differs from configured mode {expected[0]}x{expected[1]}."
                )
            return image
        except FrameError:
            raise
        except Exception as exc:
            raise FrameError(f"Cannot read camera {camera} frame: {exc}") from exc


class DemoSource:
    """Read the same offline scene for each camera; construction reads no file."""

    def __init__(self, directory: str | Path | None = None):
        self.camera_count = 4
        self.directory = (
            Path(directory)
            if directory is not None
            else Path(__file__).parents[2] / "tests" / "fixtures" / "vision"
        )

    def read(self, camera: int) -> Image.Image:
        """Return the fixture as an RGB image, or raise FrameError on failure."""
        validate_camera(camera)
        path = self.directory / "scene.ppm"
        try:
            with path.open("rb") as stream:
                data = go2rtc.read_bounded(stream, _MAX_BYTES)
            return _decode(data, "PPM")
        except FrameError:
            raise
        except Exception as exc:
            raise FrameError(f"Cannot read demo frame {path}: {exc}") from exc

    def still(self, camera: int) -> Image.Image:
        return self.read(camera)


def still(camera: int, *, settings=None, opener=None) -> Image.Image:
    """Fetch one checked frame without opening or reconfiguring a camera device."""
    return Go2RTCSource(settings=settings, opener=opener).still(camera)
