import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from email.message import Message
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Condition, Lock, Thread

from cameras.config import MJPEG_MODES, camera_configuration
from cameras.jpeg import jpeg_size
from cameras.rate import SendRate

CAMERAS = camera_configuration(os.environ)
# A side stream above 1280x720 belongs to the microscope, which runs it only during an
# objective switch. A relayed view would start it on the shared USB link and the VPN.
SIDE = os.environ.get("MG400_MICROSCOPE_SIDE_STREAM", "").strip()
STREAMS = {
    f"{stream}.jpg": stream
    for stream in CAMERAS.streams
    if stream != SIDE or stream in CAMERAS.preloaded
}
# go2rtc encodes each camera up to 1280x720 and the microscope preview to H.264 for its first
# viewer, as fragmented MP4. H.264 needs much less VPN bandwidth than MJPEG at the camera rate.
VIDEOS = {f"{stream}.mp4": stream for stream in (*CAMERAS.preloaded, "microscope")}
VIDEO_TYPE = re.compile(r'video/mp4; codecs="[A-Za-z0-9.,]{1,100}"')
MAX_FRAME = 4 * 1024 * 1024
# A viewer may cap its frame rate with ?fps=. The console asks for less than the camera
# rate, so 3 previews leave room on the VPN link for control traffic.
MAX_FPS = 30
GO2RTC = "http://127.0.0.1:1984"
# A still that changes capture mode needs the shared USB 2 link alone. go2rtc reads this
# overlay after its main config. The overlay turns off the other
# cameras and the H.264 encoders and sets one camera to the requested AC410 MJPEG mode.
# The capture unit deletes a left-over overlay before go2rtc starts.
STILL_CONFIG = Path("/var/lib/mg400/camera/still.yaml")
STILL_MODE = (4000, 3000, 15)
STILL_MAX_FRAME = 16 * 1024 * 1024
# Auto exposure settles after the mode change. Older frames are too dark or too bright.
STILL_SETTLE_S = 1.0
STILL_TIMEOUT_S = 10.0
STILLS = {f"{stream}.full.jpg": stream for stream in CAMERAS.streams}
STILL_LOCK = Lock()


def iter_mjpeg(upstream, *, keep_reading=lambda: True, max_frame=MAX_FRAME):
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
                if len(buffer) - body_start > max_frame + len(marker) + 4:
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
                or not 4 <= len(frame) <= max_frame
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


def still_mode(query):
    """Select the requested native size at its lowest supported capture rate."""
    if not query:
        return STILL_MODE
    return min(
        (mode for mode in MJPEG_MODES if query == f"size={mode[0]}x{mode[1]}"),
        key=lambda mode: mode[2],
        default=None,
    )


def still_overlay(stream, mode=STILL_MODE):
    """Return the go2rtc overlay that streams only `stream`, at the requested mode."""
    width, height, fps = mode
    lines = []
    for name, device in zip(CAMERAS.streams, CAMERAS.devices):
        source = (
            f"v4l2:device?video={device}&input_format=mjpeg"
            f"&video_size={width}x{height}&framerate={fps}"
            if name == stream
            else ""
        )
        lines.append(f'  {name}: "{source}"\n')
    lines += [f'  {name}_{codec}: ""\n' for name in CAMERAS.preloaded for codec in ("h264", "h265")]
    return "streams:\n" + "".join(lines)


def restart_go2rtc():
    """Re-exec go2rtc with its config files, then wait until its API answers again."""
    try:
        urllib.request.urlopen(
            urllib.request.Request(GO2RTC + "/api/restart", method="POST"), timeout=2
        ).close()
    except (OSError, HTTPException):
        pass  # go2rtc can exec before it sends the response.
    time.sleep(0.5)
    deadline = time.monotonic() + 5
    while True:
        try:
            urllib.request.urlopen(GO2RTC + "/api", timeout=1).close()
            return
        except (OSError, HTTPException):
            if time.monotonic() > deadline:
                raise OSError("go2rtc did not restart") from None
            time.sleep(0.1)


def microscope_busy():
    """True while a microscope job runs. An objective switch reads the side stream."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:8770/api/state", timeout=2) as response:
            return json.load(response).get("busy") is True
    except (OSError, ValueError, HTTPException):
        return False


def grab_shared_still(stream, mode):
    """Read a complete JPEG from the existing camera stream at the requested size."""
    with urllib.request.urlopen(GO2RTC + "/api/frame.jpeg?src=" + stream, timeout=5) as upstream:
        frame = upstream.read(STILL_MAX_FRAME + 1)
        length = upstream.headers.get("Content-Length")
    if (
        not 4 <= len(frame) <= STILL_MAX_FRAME
        or not frame.endswith(b"\xff\xd9")
        or (length is not None and int(length) != len(frame))
    ):
        raise ValueError("Invalid or incomplete JPEG frame")
    try:
        size = jpeg_size(frame)
    except RuntimeError as error:
        raise ValueError(str(error)) from error
    if size != mode[:2]:
        raise ValueError("The camera sent an unexpected frame size")
    return frame


def grab_still(stream, mode=STILL_MODE):
    """Return 1 settled full-resolution JPEG. The other cameras pause for a few seconds."""
    STILL_CONFIG.write_text(still_overlay(stream, mode))
    try:
        restart_go2rtc()
        start = time.monotonic()
        with urllib.request.urlopen(
            GO2RTC + "/api/stream.mjpeg?src=" + stream, timeout=5
        ) as upstream:
            for frame in iter_mjpeg(upstream, max_frame=STILL_MAX_FRAME):
                now = time.monotonic()
                if now - start > STILL_TIMEOUT_S:
                    break
                try:
                    size = jpeg_size(frame)
                except RuntimeError:
                    continue
                if now - start >= STILL_SETTLE_S and size == mode[:2]:
                    return frame
        raise OSError("The camera sent no full-resolution frame")
    finally:
        STILL_CONFIG.unlink(missing_ok=True)
        restart_go2rtc()


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
        self.sent = SendRate()

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

    def count_sent(self):
        with self.condition:
            self.sent.add(time.monotonic())

    def fps(self):
        """Frames per second that 1 MJPEG viewer gets, or None."""
        with self.condition:
            return self.sent.per_viewer(time.monotonic(), self.viewers)

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
                # No read timeout: go2rtc keeps a closed consumer until the next frame, so a
                # reconnect on each stall adds 1 consumer. Frames resume on this connection.
                with urllib.request.urlopen(
                    "http://127.0.0.1:1984/api/stream.mjpeg?src=" + self.source, timeout=None
                ) as upstream:
                    for frame in iter_mjpeg(upstream, keep_reading=self.active):
                        self.publish(frame)
            except (OSError, ValueError, UnicodeError, HTTPException):
                pass
            self.disconnect()
            if self.active():
                time.sleep(0.5)


def stream_fps(query):
    """Return the frame rate cap of 1 stream request, or None for an unknown query."""
    if not query:
        return MAX_FPS
    match = re.fullmatch(r"fps=([1-9][0-9]?)", query)
    return int(match.group(1)) if match and int(match.group(1)) <= MAX_FPS else None


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
        path, _, query = self.path.partition("?")
        parts = path.split("/")
        if len(parts) != 2:
            self.send_error(404)
            return
        route = parts[1]
        fps = stream_fps(query)
        if route in {source + ".mjpeg" for source in FEEDS} and fps:
            self.stream(FEEDS[route.removesuffix(".mjpeg")], fps)
            return
        if route in VIDEOS and query in ("", "codec=h265"):
            self.video(VIDEOS[route], hevc=bool(query))
            return
        if route == "fps.json" and not query:
            # The console shows these rates on the MJPEG tiles. H.264 tiles count their own.
            rates = {source: feed.fps() for source, feed in FEEDS.items()}
            self.respond(json.dumps(rates).encode(), "application/json")
            return
        if route in STILLS and (mode := still_mode(query)) is not None:
            self.still(STILLS[route], mode, wait=not query)
            return
        if query or route not in STREAMS:
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

    def still(self, stream, mode=STILL_MODE, *, wait=True):
        acquired = STILL_LOCK.acquire(timeout=45) if wait else STILL_LOCK.acquire(blocking=False)
        if not acquired:
            self.send_error(503, "Another still is in progress")
            return
        try:
            native_mode = CAMERAS.modes[CAMERAS.streams.index(stream)]
            if stream in CAMERAS.preloaded and native_mode[:2] == mode[:2]:
                body = grab_shared_still(stream, mode)
            else:
                # A mode change pauses every camera, including the microscope side stream.
                if SIDE and microscope_busy():
                    self.send_error(409, "The microscope is busy")
                    return
                body = grab_still(stream, mode)
        except (OSError, ValueError, UnicodeError, HTTPException):
            self.send_error(502)
            return
        finally:
            STILL_LOCK.release()
        self.respond(body, "image/jpeg")

    def stream(self, feed, fps=MAX_FPS):
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
                    feed.count_sent()
                    time.sleep(1 / fps)
                    sequence, frame = feed.wait_after(sequence)
            except OSError:
                pass
        finally:
            feed.release()

    def video(self, source, hevc=False):
        """Relay the H.265 or H.264 preview of 1 camera unchanged. go2rtc names the codec for MSE.

        A host without NVENC has no H.265 preview. The viewer then gets H.264.
        """
        upstream = None
        for codec in ("h265", "h264") if hevc else ("h264",):
            try:
                upstream = urllib.request.urlopen(
                    f"http://127.0.0.1:1984/api/stream.mp4?src={source}_{codec}", timeout=5
                )
                break
            except (OSError, HTTPException):
                continue
        if upstream is None:
            self.send_error(502)
            return
        with upstream:
            content_type = upstream.headers.get("Content-Type", "")
            if not VIDEO_TYPE.fullmatch(content_type):
                self.send_error(502)
                return
            self.close_connection = True
            self.connection.settimeout(5)
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)
            try:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Connection", "close")
                self.end_headers()
                while chunk := upstream.read1(64 * 1024):
                    self.wfile.write(chunk)
            except (OSError, HTTPException):
                pass

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
    if STILL_CONFIG.exists():
        # This process stopped during a still. Return go2rtc to the previews.
        STILL_CONFIG.unlink()
        restart_go2rtc()
    server = ThreadingHTTPServer((host, 1985), Preview)
    print(f"Camera preview is ready at {host}:1985.", flush=True)
    server.serve_forever()
