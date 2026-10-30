"""Bounded frame reads from the local go2rtc service or offline demo fixtures."""

from __future__ import annotations

import math
import time
from contextlib import closing
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

from PIL import Image, ImageOps

_MAX_BYTES = 8 * 1024 * 1024
_MAX_PIXELS = 16_000_000
_READ_CHUNK = 64 * 1024


class FrameError(RuntimeError):
    """A frame could not be read or decoded within the source limits."""


def validate_camera(camera: int) -> int:
    """Return a camera number from 1 through 3; reject other types and values."""
    if type(camera) is not int or camera not in (1, 2, 3):
        raise ValueError("camera must be an integer from 1 through 3.")
    return camera


def _read_bytes(stream, max_bytes: int, *, deadline=None, clock=time.monotonic) -> bytes:
    """Read at most the configured limit plus 1 byte to detect oversized data."""
    data = bytearray()
    # HTTPResponse.read() can fill the requested size through many socket reads.
    # read1() returns after at most one underlying read, so trickling body data
    # cannot prevent the elapsed-time check. Each read still has a socket timeout.
    read = getattr(stream, "read1", stream.read) if deadline is not None else stream.read
    while len(data) <= max_bytes:
        if deadline is not None and clock() >= deadline:
            raise FrameError("Frame body read timed out.")
        size = min(_READ_CHUNK, max_bytes + 1 - len(data))
        chunk = read(size)
        if deadline is not None and clock() >= deadline:
            raise FrameError("Frame body read timed out.")
        if not isinstance(chunk, bytes):
            raise FrameError("Frame source returned invalid byte data.")
        if len(chunk) > size:
            raise FrameError("Frame source exceeded the requested read size.")
        if not chunk:
            return bytes(data)
        data.extend(chunk)
    raise FrameError(f"Frame exceeds the {max_bytes}-byte limit.")


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


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib raises HTTPError instead of opening the redirect destination.
        return None


class Go2RTCSource:
    """Read JPEG frames from go2rtc, which remains the only camera owner.

    The optional opener is a callable or an object with an ``open`` method. It
    receives the fixed URL and ``timeout`` keyword and returns a byte stream
    with ``read(size)`` and ``close()`` methods. Test openers must not perform
    network access. The default opener bypasses proxies and refuses redirects.
    Construction opens no network connection.
    """

    def __init__(
        self,
        *,
        timeout: float = 2.0,
        max_bytes: int = _MAX_BYTES,
        opener=None,
        clock=time.monotonic,
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

    def read(self, camera: int) -> Image.Image:
        """Return an EXIF-oriented RGB frame, or raise FrameError on failure."""
        camera = validate_camera(camera)
        url = f"http://127.0.0.1:1984/api/frame.jpeg?src=camera{camera}"
        opener = self._opener
        if opener is None:
            opener = build_opener(ProxyHandler({}), _NoRedirect())
        open_frame = opener if callable(opener) else opener.open
        try:
            with closing(open_frame(url, timeout=self.timeout)) as response:
                geturl = getattr(response, "geturl", None)
                if geturl is not None and geturl() != url:
                    raise FrameError("Frame response came from a different endpoint.")
                status = getattr(response, "status", 200)
                if status != 200:
                    raise FrameError(f"Frame endpoint returned HTTP {status}.")
                data = _read_bytes(
                    response,
                    self.max_bytes,
                    deadline=self._clock() + self.timeout,
                    clock=self._clock,
                )
            return _decode(data, "JPEG")
        except FrameError:
            raise
        except HTTPError as exc:
            exc.close()
            raise FrameError(f"Cannot read camera {camera} frame: HTTP {exc.code}.") from exc
        except Exception as exc:
            raise FrameError(f"Cannot read camera {camera} frame: {exc}") from exc


class DemoSource:
    """Read the same offline scene for each camera; construction reads no file."""

    def __init__(self, directory: str | Path | None = None):
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
                data = _read_bytes(stream, _MAX_BYTES)
            return _decode(data, "PPM")
        except FrameError:
            raise
        except Exception as exc:
            raise FrameError(f"Cannot read demo frame {path}: {exc}") from exc
