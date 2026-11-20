"""Read frames from the local go2rtc camera owner; never open a camera device.

go2rtc owns every UVC camera. All readers use this module: a fixed loopback URL,
a checked stream name, no proxy, no redirect, and bounded bodies.

- read_jpeg: one bounded `api/frame.jpeg` still.
- Go2RTCCapture: the newest frame from one persistent `api/stream.mjpeg` connection.
"""

import re
import threading
import time
from contextlib import closing
from email.message import Message
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

import cv2
import numpy as np

from cameras.jpeg import jpeg_size as _jpeg_size
from labkit import ports

RECONNECT_SECONDS = 0.5
STREAM_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_READ_CHUNK = 64 * 1024


class FrameError(RuntimeError):
    """A frame could not be read or decoded within the source limits."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib raises HTTPError instead of opening the redirect destination.
        return None


def default_opener():
    """Return an opener that bypasses proxies and refuses redirects."""
    return build_opener(ProxyHandler({}), _NoRedirect())


def check_stream(stream):
    """Return a go2rtc stream name; raise ValueError for other values."""
    if not isinstance(stream, str) or not STREAM_NAME.fullmatch(stream):
        raise ValueError("A go2rtc stream name is required")
    return stream


def url(endpoint, stream):
    """Return the fixed loopback go2rtc URL for one API endpoint and stream."""
    return ports.url("go2rtc", f"api/{endpoint}?src=") + quote(check_stream(stream), safe="")


def _open(opener):
    opener = opener or default_opener()
    return opener if callable(opener) else opener.open


def read_bounded(stream, max_bytes, *, deadline=None, clock=time.monotonic):
    """Read at most max_bytes; with a deadline, check the time around each read1 call."""
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


def read_jpeg(stream, *, timeout=2.0, max_bytes, opener=None, clock=time.monotonic):
    """Return the bytes of one `api/frame.jpeg` still from a go2rtc stream.

    The opener is a callable or an object with an ``open`` method. It receives the
    fixed URL and the ``timeout`` keyword. The body budget starts after the response
    headers. Raise FrameError for a bad response or body. Network errors other than
    HTTPError pass through unchanged.
    """
    frame_url = url("frame.jpeg", stream)
    try:
        with closing(_open(opener)(frame_url, timeout=timeout)) as response:
            geturl = getattr(response, "geturl", None)
            if geturl is not None and geturl() != frame_url:
                raise FrameError("Frame response came from a different endpoint.")
            status = getattr(response, "status", 200)
            if status != 200:
                raise FrameError(f"Frame endpoint returned HTTP {status}.")
            return read_bounded(response, max_bytes, deadline=clock() + timeout, clock=clock)
    except HTTPError as exc:
        exc.close()
        raise FrameError(f"Frame endpoint returned HTTP {exc.code}.") from exc


class Go2RTCCapture:
    """Newest complete JPEG from one persistent loopback MJPEG stream, with no redirects.

    go2rtc starts a stream larger than 1280x720 only on demand, and the first frame
    after a start can be truncated. One open `api/stream.mjpeg` connection keeps the
    stream running. A reader thread drops incomplete parts and reconnects after a
    failure. read() decodes only the newest complete JPEG.
    """

    def __init__(self, stream, *, opener=None):
        self.url = url("stream.mjpeg", stream)
        self._opener = opener or default_opener()
        self._closed = threading.Event()
        self._condition = threading.Condition()
        self._jpeg = None
        self._sequence = self._returned = 0
        self._thread = None

    def read(self):
        """Wait for a newer complete JPEG and decode it. Return (False, None) after release."""
        with self._condition:
            if self._thread is None and not self._closed.is_set():
                self._thread = threading.Thread(
                    target=self._run, name="go2rtc-mjpeg-reader", daemon=True
                )
                self._thread.start()
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: self._closed.is_set() or self._sequence != self._returned
                )
                if self._closed.is_set():
                    return False, None
                self._returned, data, expected = self._jpeg
            frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is not None and (frame.shape[1], frame.shape[0]) == expected:
                return True, frame

    def release(self):
        with self._condition:
            self._closed.set()
            self._condition.notify_all()

    def _run(self):
        open_stream = _open(self._opener)
        while not self._closed.is_set():
            try:
                # No read timeout: go2rtc keeps a closed consumer until the next frame, so a
                # reconnect on each camera stall adds 1 consumer. Frames resume on this stream.
                with closing(open_stream(self.url, timeout=None)) as response:
                    self._read_parts(response)
            except HTTPError as error:
                error.close()
            except Exception:
                pass  # Drop this connection and reconnect.
            self._closed.wait(RECONNECT_SECONDS)

    def _read_parts(self, response):
        if getattr(response, "status", 200) != 200:
            raise RuntimeError("go2rtc stream response was not successful")
        if hasattr(response, "geturl") and response.geturl() != self.url:
            raise RuntimeError("go2rtc stream response changed endpoint")
        content_type = Message()
        content_type["content-type"] = response.headers.get("Content-Type", "")
        boundary = content_type.get_param("boundary")
        if (
            content_type.get_content_type() != "multipart/x-mixed-replace"
            or not isinstance(boundary, str)
            or not 0 < len(boundary) <= 70
        ):
            raise RuntimeError("go2rtc did not return an MJPEG stream")
        marker = b"--" + boundary.encode("ascii")
        while not self._closed.is_set():
            line = response.readline(80)
            if line == b"\r\n":
                continue
            if line.rstrip(b"\r\n") != marker:
                raise RuntimeError("go2rtc stream ended or lost its part boundary")
            length = header = None
            for _ in range(8):
                header = response.readline(256)
                if header in (b"\r\n", b""):
                    break
                name, _, value = header.partition(b":")
                if name.strip().lower() == b"content-length":
                    length = int(value)
            if header != b"\r\n" or length is None or not 0 < length <= 16_777_216:
                raise RuntimeError("go2rtc stream part has no valid length")
            data = response.read(length)
            if len(data) != length:
                raise RuntimeError("go2rtc stream ended inside a frame")
            if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
                continue
            try:
                expected = _jpeg_size(data)
            except RuntimeError:
                continue
            with self._condition:
                self._sequence += 1
                self._jpeg = (self._sequence, data, expected)
                self._condition.notify_all()
