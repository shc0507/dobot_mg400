"""Threaded UVC frames from the local go2rtc camera owner.

The frame publication API comes from USBCamera. Capture ownership belongs to
camera preview, so this adapter never opens a V4L2 device or OpenCV capture.
"""

import math
import os
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

RECONNECT_SECONDS = 0.5


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _jpeg_size(data):
    """Reject oversized JPEG dimensions before allocating a decoded image."""
    if not data.startswith(b"\xff\xd8"):
        raise RuntimeError("go2rtc did not return JPEG data")
    cursor = 2
    while cursor + 4 <= len(data):
        if data[cursor] != 255:
            break
        while cursor < len(data) and data[cursor] == 255:
            cursor += 1
        if cursor >= len(data):
            break
        marker = data[cursor]
        cursor += 1
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if marker in (0xD9, 0xDA) or cursor + 2 > len(data):
            break
        size = int.from_bytes(data[cursor : cursor + 2], "big")
        if size < 2 or cursor + size > len(data):
            break
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if size < 8:
                break
            height = int.from_bytes(data[cursor + 3 : cursor + 5], "big")
            width = int.from_bytes(data[cursor + 5 : cursor + 7], "big")
            if not width or not height or width * height > 16_000_000:
                raise RuntimeError("go2rtc JPEG dimensions exceed the frame limit")
            return width, height
        cursor += size
    raise RuntimeError("go2rtc returned an invalid JPEG header")


class Go2RTCCapture:
    """Newest complete JPEG from one persistent loopback MJPEG stream, with no redirects.

    go2rtc starts a stream larger than 640x480 only on demand, and the first frame
    after a start can be truncated. One open `api/stream.mjpeg` connection keeps the
    stream running. A reader thread drops incomplete parts and reconnects after a
    failure. read() decodes only the newest complete JPEG.
    """

    def __init__(self, stream, *, timeout=2.0, opener=None):
        if not isinstance(stream, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", stream):
            raise ValueError("A go2rtc stream name is required")
        if isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 10:
            raise ValueError("Frame timeout must be within 0..10 seconds")
        self.url = "http://127.0.0.1:1984/api/stream.mjpeg?src=" + quote(stream, safe="")
        self.timeout = timeout
        self._opener = opener or build_opener(ProxyHandler({}), _NoRedirect())
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
        open_stream = self._opener if callable(self._opener) else self._opener.open
        while not self._closed.is_set():
            try:
                with closing(open_stream(self.url, timeout=self.timeout)) as response:
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


class USBCamera:
    """Side-camera preview with fresh frame IDs and monotonic receive times."""

    def __init__(self, stream=None, resolution=(3840, 2160), fps=30.0, *, capture_factory=None):
        self.stream = (
            os.environ.get("MG400_MICROSCOPE_SIDE_STREAM", "") if stream is None else stream
        )
        if not isinstance(self.stream, str) or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_-]{0,63}", self.stream
        ):
            raise ValueError("Set MG400_MICROSCOPE_SIDE_STREAM to the owned go2rtc stream")
        if resolution is not None and (
            len(resolution) != 2
            or any(type(n) is not int or n <= 0 for n in resolution)
            or resolution[0] * resolution[1] > 16_000_000
        ):
            raise ValueError("Invalid expected side-camera resolution")
        if isinstance(fps, bool) or not math.isfinite(fps) or not 0 < fps <= 60:
            raise ValueError("Frame rate must be within 0..60")
        self.resolution = resolution
        self.fps = fps
        self._capture_factory = capture_factory or Go2RTCCapture
        self._capture = None
        self._thread = None
        self._lifecycle = threading.RLock()
        self._stop_event = threading.Event()
        self._frame_condition = threading.Condition()
        self._latest_frame = None
        self._frame_id = 0
        self._captured_at = None
        self._error = None
        self._frame_size = None

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
        return f"go2rtc side camera ({self.stream})"

    def open(self):
        with self._lifecycle:
            if self.is_open:
                return self
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("Previous side-camera reader is still stopping")
            self._stop_event = threading.Event()
            self._error = None
            self._latest_frame = None
            self._captured_at = None
            # IDs remain monotonic across reopen so old after_id cursors cannot
            # mistake an earlier session's frames for new frames.
            capture = self._capture_factory(self.stream)
            self._capture = capture
            self._thread = threading.Thread(
                target=self._acquisition_loop,
                args=(capture, self._stop_event),
                name="microscope-side-camera",
                daemon=True,
            )
            try:
                self._thread.start()
            except BaseException:
                self._capture = None
                capture.release()
                self._thread = None
                raise
        return self

    def _acquisition_loop(self, capture, stop):
        try:
            while not stop.is_set():
                ok, frame = capture.read()
                if not ok or frame is None:
                    if stop.is_set():
                        break
                    raise RuntimeError("The side camera did not provide a frame")
                size = (frame.shape[1], frame.shape[0])
                if self.resolution is not None and size != self.resolution:
                    raise RuntimeError("Side-camera frame size differs from its reference size")
                with self._frame_condition:
                    if stop.is_set() or capture is not self._capture:
                        return
                    self._frame_size = size
                    self._latest_frame = frame
                    self._frame_id += 1
                    self._captured_at = time.monotonic()
                    self._frame_condition.notify_all()
                stop.wait(1 / self.fps)
        except Exception as error:
            with self._frame_condition:
                if not stop.is_set() and capture is self._capture:
                    self._error = error
                self._frame_condition.notify_all()
        finally:
            with self._frame_condition:
                self._frame_condition.notify_all()

    def get_frame(self, wait=True, timeout=2.0, copy=True):
        packet = self.get_frame_packet(wait=wait, timeout=timeout, copy=copy)
        return None if packet is None else packet[2]

    def get_frame_packet(self, after_id=None, wait=True, timeout=2.0, copy=True):
        """Return (ID, monotonic receive time, frame), never a sensor timestamp."""
        deadline = time.monotonic() + timeout
        with self._frame_condition:
            while True:
                if not self.is_open:
                    raise RuntimeError("USB camera is closed")
                if self._error is not None:
                    raise RuntimeError("USB camera acquisition stopped") from self._error
                if self._latest_frame is not None and (
                    after_id is None or self._frame_id > after_id
                ):
                    frame = self._latest_frame
                    return self._frame_id, self._captured_at, frame.copy() if copy else frame
                remaining = deadline - time.monotonic()
                if not wait or remaining <= 0:
                    return None
                self._frame_condition.wait(remaining)

    def close(self):
        with self._lifecycle:
            self._stop_event.set()
            with self._frame_condition:
                capture, self._capture = self._capture, None
                self._latest_frame = None
                self._frame_condition.notify_all()
            if capture is not None:
                capture.release()
            if self._thread is not None:
                self._thread.join(timeout=3.0)
                if self._thread.is_alive():
                    raise RuntimeError("Side-camera reader is still stopping")
                self._thread = None

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
