import hmac
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from email.message import Message
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Condition, Thread

from cameras.config import camera_configuration

# An empty environment value counts as unset.
KEY_FILE = Path(os.environ.get("MG400_CAMERA_KEY_FILE") or "/var/lib/mg400/camera/preview.key")
KEY = KEY_FILE.read_bytes().strip()
if len(KEY) < 32:
    raise ValueError("The camera access key must contain at least 32 bytes.")
CAMERAS = camera_configuration(os.environ)
STREAMS = {f"{stream}.jpg": stream for stream in CAMERAS.streams}
MAX_FRAME = 4 * 1024 * 1024


def iter_mjpeg(upstream, *, keep_reading=lambda: True):
    """Read complete JPEG parts. Bound memory even if the source omits a boundary."""
    content_type = Message()
    content_type["content-type"] = upstream.headers.get("Content-Type", "")
    boundary = content_type.get_param("boundary")
    if (
        content_type.get_content_type() != "multipart/x-mixed-replace"
        or not boundary
        or len(boundary) > 70
        or "\r" in boundary
        or "\n" in boundary
    ):
        raise ValueError("Invalid MJPEG boundary")
    marker = b"--" + boundary.encode("ascii")
    buffer = bytearray()
    while keep_reading():
        chunk = upstream.read1(64 * 1024)
        if not chunk:
            if buffer.startswith(marker + b"--"):
                return
            raise ValueError("MJPEG stream ended before a complete boundary")
        buffer.extend(chunk)
        while True:
            if not buffer.startswith(marker):
                if len(buffer) < len(marker):
                    break
                raise ValueError("Invalid MJPEG part boundary")
            if buffer.startswith(marker + b"--"):
                return
            headers_end = buffer.find(b"\r\n\r\n", len(marker))
            if headers_end < 0:
                if len(buffer) > 8192:
                    raise ValueError("MJPEG headers are too large")
                break
            if headers_end > 8192:
                raise ValueError("MJPEG headers are too large")
            if buffer[len(marker) : len(marker) + 2] != b"\r\n":
                raise ValueError("Invalid MJPEG boundary line")
            body_start = headers_end + 4
            next_part = buffer.find(b"\r\n" + marker, body_start)
            while next_part >= 0:
                suffix = next_part + 2 + len(marker)
                if len(buffer) < suffix + 2:
                    next_part = -1
                    break
                if buffer[suffix : suffix + 2] in (b"\r\n", b"--"):
                    break
                next_part = buffer.find(b"\r\n" + marker, suffix)
            if next_part < 0:
                if len(buffer) - body_start > MAX_FRAME + len(marker) + 4:
                    raise ValueError("MJPEG frame is too large")
                break
            header_lines = bytes(buffer[len(marker) + 2 : headers_end]).decode("ascii")
            headers = Message()
            for line in header_lines.split("\r\n"):
                name, value = line.split(":", 1)
                headers[name] = value.strip()
            frame = bytes(buffer[body_start:next_part])
            if (
                headers.get_content_type() != "image/jpeg"
                or not 4 <= len(frame) <= MAX_FRAME
                or not frame.startswith(b"\xff\xd8")
                or not frame.endswith(b"\xff\xd9")
                or (
                    headers.get("Content-Length") is not None
                    and int(headers["Content-Length"]) != len(frame)
                )
            ):
                raise ValueError("Invalid or incomplete JPEG frame")
            del buffer[: next_part + 2]
            yield frame


class CameraFeed:
    """Share one upstream reader and one latest-frame slot between viewers."""

    def __init__(self, source, *, on_demand=False):
        self.source = source
        self.on_demand = on_demand
        self.viewers = 0
        self.condition = Condition()
        self.thread = None
        self.sequence = 0
        self.frame = None
        self.error = False
        self.received = 0.0

    def start(self):
        with self.condition:
            if self.on_demand and self.viewers == 0:
                self.error = False
            self.viewers += 1
            if self.thread is None:
                self.thread = Thread(target=self.run, daemon=True)
                try:
                    self.thread.start()
                except BaseException:
                    self.thread = None
                    self.viewers -= 1
                    raise

    def release(self):
        with self.condition:
            self.viewers = max(0, self.viewers - 1)
            if self.on_demand and self.viewers == 0:
                self.frame = None
                self.error = True
                self.condition.notify_all()

    def active(self):
        with self.condition:
            return not self.on_demand or self.viewers > 0

    def publish(self, frame):
        with self.condition:
            if self.on_demand and self.viewers == 0:
                return
            self.sequence += 1
            self.frame = frame
            self.error = False
            self.received = time.monotonic()
            self.condition.notify_all()

    def disconnect(self):
        with self.condition:
            self.frame = None
            self.error = True
            self.condition.notify_all()

    def wait_after(self, sequence, timeout=1.5):
        with self.condition:
            ready = self.condition.wait_for(
                lambda: (
                    self.error
                    or (
                        self.frame is not None
                        and self.sequence != sequence
                        and 0 <= time.monotonic() - self.received <= 1.5
                    )
                ),
                timeout,
            )
            if not ready or self.error:
                raise OSError("Camera stream is unavailable")
            return self.sequence, self.frame

    def run(self):
        while True:
            with self.condition:
                if self.on_demand and self.viewers == 0:
                    self.thread = None
                    return
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:1984/api/stream.mjpeg?src=" + self.source, timeout=2
                ) as upstream:
                    for frame in iter_mjpeg(upstream, keep_reading=self.active):
                        self.publish(frame)
            except (OSError, ValueError, UnicodeError, HTTPException):
                pass
            self.disconnect()
            if self.active():
                time.sleep(0.5)


FEEDS = {
    source: CameraFeed(source, on_demand=source not in CAMERAS.preloaded)
    for source in STREAMS.values()
}


class Preview(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'",
        )
        super().end_headers()

    def do_GET(self):
        parts = self.path.split("/")
        if len(parts) != 3 or not hmac.compare_digest(parts[1].encode(), KEY):
            self.send_error(404)
            return
        route = parts[2]
        if route in {source + ".mjpeg" for source in FEEDS}:
            self.stream(FEEDS[route.removesuffix(".mjpeg")])
            return
        if route not in STREAMS:
            self.send_error(404)
            return
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:1984/api/frame.jpeg?src=" + STREAMS[route], timeout=2
            ) as upstream:
                body = upstream.read()
        except (urllib.error.URLError, OSError):
            self.send_error(502)
            return
        self.respond(body, "image/jpeg")

    def stream(self, feed):
        feed.start()
        try:
            try:
                sequence, frame = feed.wait_after(0)
            except OSError:
                self.send_error(502)
                return
            self.close_connection = True
            self.connection.settimeout(1.5)
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    self.wfile.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(frame)).encode()
                        + b"\r\n\r\n"
                        + frame
                        + b"\r\n"
                    )
                    time.sleep(1 / 30)
                    sequence, frame = feed.wait_after(sequence)
            except OSError:
                pass
        finally:
            feed.release()

    def respond(self, body, content_type):
        try:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass


if __name__ == "__main__":
    if len(sys.argv) == 2:
        host = sys.argv[1]
    else:
        host = os.environ.get("MG400_CAMERA_BIND") or "127.0.0.1"
    server = ThreadingHTTPServer((host, 1985), Preview)
    print(f"Camera preview is ready at {host}:1985.", flush=True)
    server.serve_forever()
