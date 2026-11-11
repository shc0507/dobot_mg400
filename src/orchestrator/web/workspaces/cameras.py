"""Console workspace: Cameras.

The browser loads camera1 to camera4 from the proxy at ``/camera/<view>.mjpeg``
and ``/camera/<view>.jpg``, and the H.264 views at ``/camera/<view>.mp4``. The
console process relays the other views:

- thermal: the thermal frame API through the thermal service client;
- microscope: the Toupcam MJPEG fallback and the full-size PNG still through the
  microscope service client;
- camera 4 chip detections: the vision ``api/detect`` result.

The microscope relay bounds its open streams. One upstream reader is shared by
its viewers; a viewer gets only the newest frame, at most at the requested
frame rate, and never a backlog. The reader closes the upstream connection when
the last viewer leaves.
"""

import base64
import json
import math
import re
import secrets
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from flask import Response, jsonify, request

from cameras.config import camera_configuration
from cameras.rate import SendRate
from labkit import ports
from labkit.errors import ServiceError

from . import Workspace

CAMERAS = ("camera1", "camera2", "camera3", "camera4")
VIEWS = (*CAMERAS, "thermal", "microscope")
MJPEG = ("microscope",)
# The microscope service allows 4 preview streams. Keep 2 for its own panel.
MAX_MICROSCOPE_STREAMS = 2
STALE_S = 2.0
RECENT_S = 5.0
# The tile polls every 40 ms. Viewers within 40 ms share 1 upstream read.
THERMAL_CACHE_S = 0.04
DETECTION_CACHE_S = 1.0
# The camera preview reports the MJPEG frames per second that each viewer of camera1 to
# camera4 gets. H.264 tiles count their frames in the browser.
PREVIEW_FPS_URL = ports.url("camera", "fps.json")
PREVIEW_FPS_CACHE_S = 0.5
MAX_FRAME = 8 * 1024 * 1024
# A lossless full-size microscope still, 2592x1944 RGB, is about 15 MB as PNG.
MAX_STILL = 80 * 1024 * 1024
MAX_JSON = 4 * 1024 * 1024
CHUNK = 64 * 1024
UPSTREAM_TIMEOUT_S = 5.0
# A stream may cap its frame rate with ?fps=. Grid and strip tiles ask for TILE_FPS.
TILE_FPS = 10
MAX_FPS = 30
PART_BOUNDARY = b"frame"
_BOUNDARY = re.compile(r'boundary="?([A-Za-z0-9\'()+_,./:=?-]{1,70})"?')


class SourceError(Exception):
    """The upstream view is unavailable. The text never contains a secret."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _open(url, timeout=UPSTREAM_TIMEOUT_S):
    headers = {"Accept": "*/*"}
    try:
        return _OPENER.open(urllib.request.Request(url, headers=headers), timeout=timeout)
    except urllib.error.HTTPError as error:
        error.close()
        raise SourceError(f"The source answered HTTP {error.code}.") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise SourceError("The source did not answer.") from None


def _read(response, limit):
    with response:
        body = response.read(limit + 1)
    if len(body) > limit:
        raise SourceError("The source response is too large.")
    return body


class HttpSources:
    """Upstream access from the console settings. Each call opens one connection."""

    def __init__(self, service):
        self.service = service

    @property
    def settings(self):
        return getattr(self.service, "settings", {}) or {}

    def _client(self, name):
        try:
            adapter = self.service.dashboard_client(name, "console-cameras-" + secrets.token_hex(6))
        except (ValueError, ServiceError):
            return None
        return getattr(adapter, "_http", None)

    def configured(self, view):
        if view in CAMERAS:
            try:
                return view in camera_configuration(self.settings).streams
            except ValueError:
                return False
        try:
            return bool(self.service.dashboard_configured(view))
        except (AttributeError, ValueError):
            return False

    def modes(self):
        try:
            config = camera_configuration(self.settings)
        except ValueError:
            return {}
        result = {f"camera{i}": m for i, m in enumerate(config.modes, 1)}
        result["thermal"] = (160, 120, None)
        return result

    def preview_fps(self):
        try:
            payload = json.loads(_read(_open(PREVIEW_FPS_URL, timeout=1.0), MAX_JSON))
        except (SourceError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {view: payload[view] for view in CAMERAS if _finite(payload.get(view))}

    def open_stream(self, view):
        http = self._client("microscope")
        if http is None:
            raise SourceError("The microscope service is not configured.")
        return _open(http.base_url + "api/camera/microscope/mjpeg")

    def still(self, view):
        http = self._client("microscope")
        if http is None:
            raise SourceError("The microscope service is not configured.")
        response = _open(http.base_url + "api/camera/microscope/still.png")
        if response.status != 200:
            response.close()
            raise SourceError("The microscope has no frame.")
        return _read(response, MAX_STILL)

    def _json(self, name, path):
        http = self._client(name)
        if http is None:
            raise SourceError(f"The {name} service is not configured.")
        body = _read(_open(http.base_url + path), MAX_JSON)
        try:
            value = json.loads(body, parse_constant=_reject)
        except ValueError:
            raise SourceError(f"The {name} service returned invalid JSON.") from None
        if not isinstance(value, dict):
            raise SourceError(f"The {name} service returned invalid JSON.")
        return value

    def thermal_frame(self):
        return self._json("thermal", "api/frame")

    def thermal_describe(self):
        return self._json("thermal", "api/describe")

    def detections(self, camera):
        return self._json("vision", "api/detect?" + urllib.parse.urlencode({"camera": camera}))


def _reject(value):
    raise ValueError("JSON numbers must be finite.")


class _Slot:
    def __init__(self, relay, view):
        self.relay, self.view, self.closed = relay, view, False
        self.lock = threading.Lock()

    def release(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
        self.relay._release(self.view)


class Relay:
    """Stream bounds and per-view frame times."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.open = dict.fromkeys(VIEWS, 0)
        self.frame_at = {}
        self.error = {}
        self.started = 0
        self.feeds = {view: _Feed(view, self) for view in MJPEG}
        self.sent = {view: SendRate() for view in MJPEG}

    # Streams -----------------------------------------------------------------

    def acquire(self, view):
        with self.lock:
            if self.open[view] >= MAX_MICROSCOPE_STREAMS:
                raise ServiceError(503, "The camera stream limit is reached. Close another view.")
            self.open[view] += 1
            self.started += 1
        return _Slot(self, view)

    def _release(self, view):
        with self.lock:
            self.open[view] = max(0, self.open[view] - 1)

    def saw_frame(self, view, at=None):
        with self.lock:
            self.frame_at[view] = self.clock() if at is None else at
            self.error.pop(view, None)

    def sent_frame(self, view, at=None):
        with self.lock:
            self.sent[view].add(self.clock() if at is None else at)

    def failed(self, view, message):
        with self.lock:
            self.error[view] = (message, self.clock())

    def streams(self):
        with self.lock:
            return dict(self.open)

    def view_state(self, view):
        now = self.clock()
        with self.lock:
            opened = self.open[view]
            seen = self.frame_at.get(view)
            error = self.error.get(view)
            fps = self.sent[view].per_viewer(now, opened) if view in self.sent else None
        age = None if seen is None else max(0.0, now - seen)
        if error is not None and (seen is None or error[1] >= seen):
            state = "offline"
        elif age is None or (not opened and age > RECENT_S):
            state = "offline"
        else:
            state = "live" if age <= STALE_S else "stale"
        return {
            "state": state,
            "age_s": None if age is None else round(age, 1),
            "streams": opened,
            "fps": fps,
            "error": None if error is None else error[0],
        }


def _content_length(headers):
    for line in headers.split(b"\r\n"):
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            value = value.strip()
            if not value.isdigit() or int(value) > MAX_FRAME:
                raise SourceError("The source sent an invalid frame length.")
            return int(value)
    return None


def _jpegs(chunks, marker):
    """Yield each complete JPEG part of a multipart stream. Bound the buffer.

    A part with Content-Length is complete after that many bytes. A part
    without it is complete at the next boundary.
    """
    buffer = b""
    for chunk in chunks:
        buffer += chunk
        while True:
            start = buffer.find(marker)
            if start < 0:
                buffer = buffer[-(len(marker) - 1) :]
                break
            buffer = buffer[start:]
            head_end = buffer.find(b"\r\n\r\n", len(marker))
            if head_end < 0:
                if len(buffer) > 8192:
                    raise SourceError("The source sent invalid frame headers.")
                break
            body = head_end + 4
            length = _content_length(buffer[len(marker) : head_end])
            if length is not None:
                if len(buffer) < body + length:
                    break
                frame, buffer = buffer[body : body + length], buffer[body + length :]
            else:
                end = buffer.find(b"\r\n" + marker, body)
                if end < 0:
                    break
                frame, buffer = buffer[body:end], buffer[end + 2 :]
            if frame.startswith(b"\xff\xd8") and frame.endswith(b"\xff\xd9"):
                yield frame
        if len(buffer) > MAX_FRAME + 8192:
            raise SourceError("The source frame is too large.")


def _part(frame):
    return (
        b"--"
        + PART_BOUNDARY
        + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
        + str(len(frame)).encode("ascii")
        + b"\r\n\r\n"
        + frame
        + b"\r\n"
    )


class _Feed:
    """One upstream reader per view. Its viewers share only the newest frame.

    A viewer never gets a backlog: when it is slow, it skips to the newest frame.
    The reader stops and closes the upstream when the last viewer leaves.
    """

    def __init__(self, view, relay):
        self.view, self.relay = view, relay
        self.condition = threading.Condition()
        self.viewers = 0
        self.upstream = None
        self.generation = 0
        self.done = True
        self.sequence = 0
        self.frame = None

    def join(self, open_upstream):
        """Count one viewer. Open the upstream if no reader runs. Return the generation."""
        with self.condition:
            self.viewers += 1
            if not self.done:
                return self.generation
        try:
            upstream, marker = open_upstream()
        except BaseException:
            with self.condition:
                self.viewers -= 1
            raise
        extra = None
        with self.condition:
            if not self.done:
                # Another viewer opened the view at the same time: use its reader.
                extra = upstream
            else:
                self.upstream, self.done, self.frame = upstream, False, None
                self.generation += 1
                generation = self.generation
                reader = threading.Thread(
                    target=self._read,
                    args=(upstream, marker, generation),
                    name="console-camera-" + self.view,
                    daemon=True,
                )
            result = self.generation
        if extra is not None:
            _close(extra)
            return result
        try:
            reader.start()
        except BaseException:
            self._end(generation)
            with self.condition:
                self.viewers -= 1
            raise
        return result

    def leave(self):
        with self.condition:
            self.viewers = max(0, self.viewers - 1)
            if self.viewers:
                return
            generation = self.generation
        self._end(generation)

    def _end(self, generation):
        with self.condition:
            if self.generation != generation or self.done:
                return False
            upstream, self.upstream, self.done = self.upstream, None, True
            self.condition.notify_all()
        _close(upstream)
        return True

    def _read(self, upstream, marker, generation):
        try:
            for frame in _jpegs(_iter_response(upstream), marker):
                with self.condition:
                    if self.generation != generation or self.done:
                        return
                    self.sequence += 1
                    self.frame = frame
                    self.condition.notify_all()
                self.relay.saw_frame(self.view)
        except (OSError, SourceError, ValueError):
            if self._end(generation):
                self.relay.failed(self.view, "The stream ended.")
        finally:
            self._end(generation)

    def next(self, generation, last, timeout):
        """Return (sequence, frame) newer than last, or None when the reader ended."""
        with self.condition:
            self.condition.wait_for(
                lambda: (
                    (self.frame is not None and self.sequence != last)
                    or self.generation != generation
                    or self.done
                ),
                timeout,
            )
            if self.generation == generation and self.frame is not None and self.sequence != last:
                return self.sequence, self.frame
            return None


def _close(upstream):
    try:
        upstream.close()
    except OSError:
        pass


def _iter_response(response):
    while True:
        chunk = response.read1(CHUNK) if hasattr(response, "read1") else response.read(CHUNK)
        if not chunk:
            return
        yield chunk


def _view(name):
    if name not in VIEWS:
        raise ServiceError(404, "Unknown camera view.")
    return name


def _mode_text(mode):
    # The tiles show the measured frame rate, not the configured one.
    if not mode:
        return None
    width, height, _ = mode
    return f"{width}×{height}"


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _thermal(payload, full):
    """Keep the display fields and compact temperatures. Celsius, raw and IR only for export."""
    temperatures = payload.get("temperatures_c")
    if (
        not isinstance(temperatures, list)
        or len(temperatures) != 120
        or any(not isinstance(row, list) or len(row) != 160 for row in temperatures)
    ):
        raise SourceError("The thermal frame has an unexpected shape.")
    result = {
        key: payload.get(key)
        for key in ("sequence", "timestamp", "source", "width", "height", "stats")
    }
    if full:
        result["temperatures_c"] = temperatures
        result["raw"] = payload.get("raw")
        result["ir"] = payload.get("ir")
        result["raw_unit"] = payload.get("raw_unit")
        return result
    # The display gets the sensor units, 1/64 kelvin, as little-endian uint16 rows in base64.
    # Celsius = value / 64 - 273.15. This is the sensor resolution and 1/7 of the JSON size.
    try:
        values = [min(65535, max(0, round((t + 273.15) * 64))) for row in temperatures for t in row]
    except TypeError:
        raise SourceError("The thermal frame has an unexpected shape.") from None
    result["temperatures_u16"] = base64.b64encode(struct.pack("<19200H", *values)).decode("ascii")
    return result


def _frame_age(timestamp, now):
    try:
        moment = datetime.fromisoformat(str(timestamp))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, now - moment.timestamp())


def _detections(payload, camera):
    size = payload.get("image_size")
    items = payload.get("detections")
    if (
        payload.get("camera") != camera
        or not isinstance(size, dict)
        or not all(type(size.get(axis)) is int and size[axis] > 0 for axis in ("width", "height"))
        or not isinstance(items, list)
    ):
        return {"available": False}
    result = []
    for item in items[:128]:
        box = item.get("xyxy") if isinstance(item, dict) else None
        if (
            not isinstance(box, list)
            or len(box) != 4
            or not all(_finite(value) for value in box)
            or not _finite(item.get("confidence"))
        ):
            continue
        result.append(
            {
                "label": str(item.get("label", ""))[:40],
                "confidence": float(item["confidence"]),
                "xyxy": [float(value) for value in box],
            }
        )
    return {
        "available": True,
        "camera": camera,
        "image_size": {"width": size["width"], "height": size["height"]},
        "detections": result,
    }


class _Cache:
    def __init__(self, clock, seconds):
        self.clock, self.seconds = clock, seconds
        self.lock = threading.Lock()
        self.value, self.at, self.error = None, -math.inf, None

    def get(self, load):
        # One upstream read at a time; concurrent viewers share the result.
        with self.lock:
            if self.clock() - self.at < self.seconds:
                if self.error is not None:
                    raise SourceError(self.error)
                return self.value
            try:
                self.value, self.error = load(), None
            except SourceError as error:
                self.value, self.error = None, str(error)
                raise
            finally:
                self.at = self.clock()
            return self.value


def register(blueprint, service):
    clock = getattr(getattr(service, "console", None), "clock", time.monotonic)
    relay = Relay(clock)
    service.camera_relay = relay
    thermal_cache = _Cache(clock, THERMAL_CACHE_S)
    detection_cache = _Cache(clock, DETECTION_CACHE_S)
    fps_cache = _Cache(clock, PREVIEW_FPS_CACHE_S)

    def sources():
        # Tests and demos may attach their own sources to the service.
        value = getattr(service, "camera_sources", None)
        return value if value is not None else HttpSources(service)

    @blueprint.get("/status")
    def status():
        source = sources()
        modes = source.modes()
        load_fps = getattr(source, "preview_fps", None)
        preview_fps = fps_cache.get(load_fps) if load_fps else {}
        views = []
        for view in VIEWS:
            row = {"id": view, "configured": source.configured(view)}
            if view in CAMERAS:
                # The browser loads these streams from the proxy; it shows their state.
                row["url"] = f"/camera/{view}.mjpeg"
                row["fps"] = preview_fps.get(view)
            else:
                row.update(relay.view_state(view))
            row["mode"] = _mode_text(modes.get(view))
            views.append(row)
        return jsonify(
            views=views,
            limits={"microscope": MAX_MICROSCOPE_STREAMS},
        )

    def stream(view):
        _view(view)
        if view not in MJPEG:
            raise ServiceError(404, "This view has no stream.")
        fps = request.args.get("fps", str(TILE_FPS))
        if set(request.args) - {"fps"} or not fps.isdecimal() or not 1 <= int(fps) <= MAX_FPS:
            raise ValueError(f"fps must be an integer from 1 to {MAX_FPS}.")
        fps = int(fps)
        slot = relay.acquire(view)
        feed = relay.feeds[view]

        def open_upstream():
            upstream = sources().open_stream(view)
            content_type = upstream.headers.get("Content-Type", "")
            match = _BOUNDARY.search(content_type)
            if not content_type.lower().startswith("multipart/x-mixed-replace") or not match:
                _close(upstream)
                raise SourceError("The source did not send an MJPEG stream.")
            return upstream, b"--" + match.group(1).encode("ascii")

        try:
            generation = feed.join(open_upstream)
        except SourceError as error:
            slot.release()
            relay.failed(view, str(error))
            raise ServiceError(502, str(error)) from None
        except BaseException:
            slot.release()
            raise
        closed = threading.Event()

        def close():
            if closed.is_set():
                return
            closed.set()
            feed.leave()
            slot.release()

        def body():
            # Send only the newest frame, at most fps a second. A slow viewer skips frames.
            interval, last, due = 1.0 / fps, None, 0.0
            try:
                while not closed.is_set():
                    wait = due - time.monotonic()
                    if wait > 0:
                        time.sleep(wait)
                    item = feed.next(generation, last, UPSTREAM_TIMEOUT_S)
                    if item is None:
                        return
                    last, frame = item
                    due = time.monotonic() + interval
                    relay.sent_frame(view)
                    yield _part(frame)
            finally:
                close()

        response = Response(
            body(),
            content_type="multipart/x-mixed-replace; boundary=" + PART_BOUNDARY.decode("ascii"),
        )
        # A response can close before its body runs: release the slot then too.
        response.call_on_close(close)
        response.headers["X-Accel-Buffering"] = "no"
        response.headers["Cache-Control"] = "no-store"
        return response

    def snapshot(view):
        _view(view)
        if view not in MJPEG:
            raise ServiceError(404, "This view has no relayed snapshot.")
        try:
            body = sources().still(view)
        except SourceError as error:
            relay.failed(view, str(error))
            raise ServiceError(502, str(error)) from None
        relay.saw_frame(view)
        name = f"{view}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.png"
        return Response(
            body,
            mimetype="image/png",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    blueprint.add_url_rule("/stream/<view>", "stream", stream, methods=["GET"])
    blueprint.add_url_rule("/snapshot/<view>", "snapshot", snapshot, methods=["GET"])

    @blueprint.get("/thermal/frame")
    def thermal_frame():
        if set(request.args) - {"full"} or request.args.get("full", "0") not in {"0", "1"}:
            raise ValueError("Use full=0 or full=1 only.")
        full = request.args.get("full") == "1"
        try:
            payload = (
                sources().thermal_frame() if full else thermal_cache.get(sources().thermal_frame)
            )
            result = _thermal(payload, full)
        except SourceError as error:
            relay.failed("thermal", str(error))
            raise ServiceError(502, str(error)) from None
        age = _frame_age(result.get("timestamp"), time.time())
        if age is not None:
            relay.saw_frame("thermal", clock() - age)
        result["age_s"] = None if age is None else round(age, 2)
        return jsonify(result)

    @blueprint.get("/thermal/regions")
    def thermal_regions():
        try:
            value = sources().thermal_describe()
        except SourceError as error:
            raise ServiceError(502, str(error)) from None
        regions = value.get("regions")
        result = {}
        if isinstance(regions, dict):
            for name, bounds in list(regions.items())[:16]:
                if isinstance(bounds, dict) and all(
                    type(bounds.get(key)) is int for key in ("x0", "y0", "x1", "y1")
                ):
                    result[str(name)[:64]] = {key: bounds[key] for key in ("x0", "y0", "x1", "y1")}
        return jsonify(regions=result)

    @blueprint.get("/detections/<int:camera>")
    def detections(camera):
        if camera != 4:
            raise ServiceError(404, "Chip detections are relayed for camera 4 only.")
        try:
            payload = detection_cache.get(lambda: sources().detections(camera))
        except SourceError:
            return jsonify(available=False)
        return jsonify(_detections(payload, camera))


WORKSPACE = Workspace(
    id="cameras", order=30, title={"en": "Cameras", "zh": "相机"}, register=register
)


__all__ = ["HttpSources", "Relay", "SourceError", "VIEWS", "WORKSPACE"]
