import hmac
import json
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

# An empty environment value counts as unset.
KEY_FILE = Path(os.environ.get("MG400_CAMERA_KEY_FILE") or "/var/lib/mg400/camera/preview.key")
KEY = KEY_FILE.read_bytes().strip()
if len(KEY) < 32:
    raise ValueError("The camera access key must contain at least 32 bytes.")
STREAMS = {f"camera{i}.jpg": f"camera{i}" for i in range(1, 4)}
MAX_FRAME = 4 * 1024 * 1024
MAX_DETECTIONS = 256 * 1024
SCRIPT = Path(__file__).with_suffix(".js").read_bytes()
PAGE = (
    """<!doctype html>
<html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MG400 摄像头</title>
<style>
body{margin:0;padding:32px;background:#111820;color:#e8edf4;font:16px system-ui}
h1{font-size:26px;margin:0 0 8px}p{color:#a6b5c6;margin:0 0 28px}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr));gap:20px}
figure{position:relative;margin:0;background:#1c2733;border:1px solid #334255;
border-radius:12px;overflow:hidden}
figure[data-paused]::before{content:"画面已暂停 · 非实时";position:absolute;top:12px;left:12px;
padding:8px 12px;border-radius:6px;background:#8a4200;color:#fff;font-weight:600;z-index:2}
.view{position:relative}canvas{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
img{display:block;width:100%;aspect-ratio:4/3;object-fit:contain;background:#080d12}
figcaption{padding:16px}small{display:block;color:#a6b5c6;margin-top:6px}
label{display:block;margin-top:10px}input{margin-right:6px}[data-overlay-status]{margin-left:8px;color:#a6b5c6}
</style>
<h1>MG400 摄像头</h1><p>AC410 · 每路最高 30 帧/秒 · 下方显示实际更新帧率和分辨率</p><main>
"""
    + "".join(
        f'<figure><div class="view"><img data-source="camera{i}.mjpeg" '
        f'data-detections="camera{i}/detections.json" alt="摄像头 {i} 暂无画面">'
        '<canvas hidden aria-hidden="true"></canvas></div>'
        f"<figcaption>摄像头 {i}<small>正在连接…</small>"
        '<label><input type="checkbox" data-overlay>识别叠加'
        '<span data-overlay-status role="status"></span></label></figcaption></figure>'
        for i in range(1, 4)
    )
    + ('</main><script src="preview.js" defer></script></html>')
)


def iter_mjpeg(upstream):
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
    while True:
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

    def __init__(self, source):
        self.source = source
        self.condition = Condition()
        self.thread = None
        self.sequence = 0
        self.frame = None
        self.error = False
        self.received = 0.0

    def start(self):
        with self.condition:
            if self.thread is None:
                self.thread = Thread(target=self.run, daemon=True)
                self.thread.start()

    def publish(self, frame):
        with self.condition:
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
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:1984/api/stream.mjpeg?src=" + self.source, timeout=2
                ) as upstream:
                    for frame in iter_mjpeg(upstream):
                        self.publish(frame)
            except (OSError, ValueError, UnicodeError, HTTPException):
                pass
            self.disconnect()
            time.sleep(0.5)


FEEDS = {source: CameraFeed(source) for source in STREAMS.values()}


class NoVisionRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never send the vision token to a redirect destination.
        return None


def vision_detections(camera):
    """Read bounded JSON from the fixed local service without affecting video."""
    unavailable = {"available": False}
    url = f"http://127.0.0.1:8771/api/detect?camera={camera}"
    try:
        headers = {"Accept": "application/json"}
        token = os.environ.get("MG400_VISION_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoVisionRedirect())
        with opener.open(urllib.request.Request(url, headers=headers), timeout=2) as response:
            if response.status != 200 or response.geturl() != url:
                return unavailable
            body = response.read(MAX_DETECTIONS + 1)
        if len(body) > MAX_DETECTIONS:
            return unavailable
        result = json.loads(body)
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("detections"), list)
            or type(result.get("camera")) is not int
            or result["camera"] != camera
            or result.get("coordinate_system") != "oriented_original_image_pixels"
            or not isinstance(result.get("image_size"), dict)
            or any(
                type(result["image_size"].get(axis)) is not int
                or not 0 < result["image_size"][axis] <= 16_000_000
                for axis in ("width", "height")
            )
            or result["image_size"]["width"] * result["image_size"]["height"] > 16_000_000
        ):
            return unavailable
        # Reject nonfinite values before copying JSON into a browser response.
        result = {**result, "available": True}
        encoded = json.dumps(result, allow_nan=False, separators=(",", ":"))
        return result if len(encoded) <= MAX_DETECTIONS else unavailable
    except urllib.error.HTTPError as error:
        error.close()
        return unavailable
    except Exception:
        # Vision is optional: bad settings, transport errors, and invalid JSON
        # must leave the independent camera stream available.
        return unavailable


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
            "default-src 'none'; img-src blob:; script-src 'self'; "
            "connect-src 'self'; style-src 'unsafe-inline'",
        )
        super().end_headers()

    def do_GET(self):
        parts = self.path.split("/")
        if len(parts) not in (3, 4) or not hmac.compare_digest(parts[1].encode(), KEY):
            self.send_error(404)
            return
        if len(parts) == 4:
            if parts[2] not in FEEDS or parts[3] != "detections.json":
                self.send_error(404)
                return
            body = json.dumps(
                vision_detections(int(parts[2][-1])), allow_nan=False, separators=(",", ":")
            ).encode()
            self.respond(body, "application/json; charset=utf-8")
            return
        route = parts[2]
        if route in ("", "preview.js"):
            body = PAGE.encode() if route == "" else SCRIPT
            content_type = "text/html" if route == "" else "text/javascript"
            self.respond(body, content_type + "; charset=utf-8")
            return
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
