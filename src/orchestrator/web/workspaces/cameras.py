"""Console workspace: Cameras.

The console process relays camera video and frames, so the browser never gets
the camera preview key or a service token:

- camera1 to camera4: the camera preview MJPEG and JPEG (``MG400_ORCH_CAMERA_URL``,
  key from ``MG400_CAMERA_KEY_FILE``);
- thermal: the thermal frame API through the thermal service client;
- microscope: the Toupcam MJPEG and JPEG through the microscope service client;
- camera 4 chip detections: the vision ``api/detect`` result.

An ``<img>`` cannot send the operator token. A page with the token asks for a
ticket (``POST ticket``). A ticket is random, single use, bound to one view and
one kind (stream or snapshot) and expires after ``TICKET_TTL_S``. The stream and
snapshot routes accept a valid ticket instead of the token. The relay bounds the
open streams and closes the upstream connection when the viewer leaves.
"""

import json
import math
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from flask import Response, jsonify, request

from cameras.config import camera_configuration
from labkit.errors import ServiceError

from . import Workspace

CAMERAS = ("camera1", "camera2", "camera3", "camera4")
VIEWS = (*CAMERAS, "thermal", "microscope")
MJPEG = (*CAMERAS, "microscope")
MAX_STREAMS = 6
MAX_STREAMS_PER_VIEW = 2
# The microscope service allows 4 preview streams. Keep 2 for its own panel.
MAX_MICROSCOPE_STREAMS = 2
TICKET_TTL_S = 20.0
MAX_TICKETS = 64
STALE_S = 2.0
RECENT_S = 5.0
THERMAL_CACHE_S = 0.2
DETECTION_CACHE_S = 1.0
MAX_FRAME = 8 * 1024 * 1024
MAX_JSON = 4 * 1024 * 1024
CHUNK = 64 * 1024
UPSTREAM_TIMEOUT_S = 5.0
DEFAULT_KEY_FILE = "/var/lib/mg400/camera/preview.key"
_BOUNDARY = re.compile(r'boundary="?([A-Za-z0-9\'()+_,./:=?-]{1,70})"?')


class SourceError(Exception):
    """The upstream view is unavailable. The text never contains a secret."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _open(url, token=None, timeout=UPSTREAM_TIMEOUT_S):
    headers = {"Accept": "*/*"}
    if token:
        headers["Authorization"] = "Bearer " + token
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

    def _preview(self):
        base = self.settings.get("MG400_ORCH_CAMERA_URL", "")
        if not base:
            return None
        try:
            key = Path(self.settings.get("MG400_CAMERA_KEY_FILE") or DEFAULT_KEY_FILE).read_bytes()
        except OSError:
            raise SourceError("The camera preview key is not readable.") from None
        key = key.strip().decode("ascii", "replace")
        if len(key) < 32 or not re.fullmatch(r"[A-Za-z0-9_.~-]+", key):
            raise SourceError("The camera preview key is not valid.")
        return base.rstrip("/") + "/" + key + "/"

    def _client(self, name):
        try:
            adapter = self.service.dashboard_client(name, "console-cameras-" + secrets.token_hex(6))
        except (ValueError, ServiceError):
            return None
        return getattr(adapter, "_http", None)

    def configured(self, view):
        if view in CAMERAS:
            return bool(self.settings.get("MG400_ORCH_CAMERA_URL"))
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

    def open_stream(self, view):
        if view in CAMERAS:
            base = self._preview()
            if base is None:
                raise SourceError("The camera preview is not configured.")
            return _open(base + view + ".mjpeg")
        http = self._client("microscope")
        if http is None:
            raise SourceError("The microscope service is not configured.")
        return _open(http.base_url + "api/camera/microscope/mjpeg", http.token)

    def frame(self, view):
        if view in CAMERAS:
            base = self._preview()
            if base is None:
                raise SourceError("The camera preview is not configured.")
            return _read(_open(base + view + ".jpg"), MAX_FRAME)
        http = self._client("microscope")
        if http is None:
            raise SourceError("The microscope service is not configured.")
        response = _open(http.base_url + "api/camera/microscope/frame.jpg", http.token)
        if response.status != 200:
            response.close()
            raise SourceError("The microscope has no frame.")
        return _read(response, MAX_FRAME)

    def _json(self, name, path):
        http = self._client(name)
        if http is None:
            raise SourceError(f"The {name} service is not configured.")
        body = _read(_open(http.base_url + path, http.token), MAX_JSON)
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
    """Tickets, stream bounds and per-view frame times."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.tickets = {}
        self.open = dict.fromkeys(VIEWS, 0)
        self.frame_at = {}
        self.error = {}
        self.started = 0

    # Tickets -----------------------------------------------------------------

    def issue(self, view, kind):
        now = self.clock()
        with self.lock:
            self.tickets = {k: v for k, v in self.tickets.items() if v[2] > now}
            if len(self.tickets) >= MAX_TICKETS:
                raise ServiceError(429, "Too many open camera tickets. Wait and try again.")
            ticket = secrets.token_urlsafe(24)
            self.tickets[ticket] = (view, kind, now + TICKET_TTL_S)
        return ticket

    def redeem(self, view, kind):
        values = request.args.getlist("ticket")
        if set(request.args) != {"ticket"} or len(values) != 1:
            raise ServiceError(401, "A camera ticket is required.")
        now = self.clock()
        with self.lock:
            entry = self.tickets.pop(values[0], None)
        if entry is None or entry[2] <= now or entry[:2] != (view, kind):
            raise ServiceError(401, "The camera ticket is not valid.")

    # Streams -----------------------------------------------------------------

    def acquire(self, view):
        with self.lock:
            total = sum(self.open.values())
            limit = MAX_MICROSCOPE_STREAMS if view == "microscope" else MAX_STREAMS_PER_VIEW
            if total >= MAX_STREAMS or self.open[view] >= limit:
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
            "error": None if error is None else error[0],
        }


def _frames(chunks, marker, on_frame):
    """Pass the chunks through and call on_frame once per multipart boundary."""
    tail = b""
    for chunk in chunks:
        data = tail + chunk
        count = data.count(marker)
        if count:
            on_frame()
        tail = data[-(len(marker) - 1) :] if len(marker) > 1 else b""
        yield chunk


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
    if not mode:
        return None
    width, height, fps = mode
    return f"{width}×{height}" + (f" · {fps} fps" if fps else "")


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _thermal(payload, full):
    """Keep the display fields; raw and IR only for the raw export."""
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
    result["temperatures_c"] = temperatures
    if full:
        result["raw"] = payload.get("raw")
        result["ir"] = payload.get("ir")
        result["raw_unit"] = payload.get("raw_unit")
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

    def sources():
        # Tests and demos may attach their own sources to the service.
        value = getattr(service, "camera_sources", None)
        return value if value is not None else HttpSources(service)

    @blueprint.get("/status")
    def status():
        source = sources()
        modes = source.modes()
        views = []
        for view in VIEWS:
            row = {"id": view, "configured": source.configured(view)}
            row.update(relay.view_state(view))
            row["mode"] = _mode_text(modes.get(view))
            views.append(row)
        return jsonify(
            views=views,
            limits={
                "streams": MAX_STREAMS,
                "per_view": MAX_STREAMS_PER_VIEW,
                "microscope": MAX_MICROSCOPE_STREAMS,
            },
            ticket_ttl_s=TICKET_TTL_S,
        )

    @blueprint.post("/ticket")
    def ticket():
        data = request.get_json()
        if set(data) != {"view", "kind"} or data["kind"] not in {"stream", "snapshot"}:
            raise ValueError("Send view and kind (stream or snapshot).")
        view = _view(data["view"])
        if view not in MJPEG:
            raise ValueError("Only camera1 to camera4 and microscope use tickets.")
        value = relay.issue(view, data["kind"])
        path = "stream" if data["kind"] == "stream" else "snapshot"
        return jsonify(
            url=f"{blueprint.url_prefix}/{path}/{view}?ticket={value}",
            expires_s=TICKET_TTL_S,
        )

    def stream(view):
        _view(view)
        if view not in MJPEG:
            raise ServiceError(404, "This view has no stream.")
        relay.redeem(view, "stream")
        slot = relay.acquire(view)
        try:
            upstream = sources().open_stream(view)
            content_type = upstream.headers.get("Content-Type", "")
            match = _BOUNDARY.search(content_type)
            if not content_type.lower().startswith("multipart/x-mixed-replace") or not match:
                upstream.close()
                raise SourceError("The source did not send an MJPEG stream.")
        except SourceError as error:
            slot.release()
            relay.failed(view, str(error))
            raise ServiceError(502, str(error)) from None
        except BaseException:
            slot.release()
            raise
        marker = b"--" + match.group(1).encode("ascii")
        closed = threading.Event()

        def close():
            if closed.is_set():
                return
            closed.set()
            try:
                upstream.close()
            except OSError:
                pass
            slot.release()

        def body():
            try:
                yield from _frames(_iter_response(upstream), marker, lambda: relay.saw_frame(view))
            except (OSError, SourceError, ValueError):
                relay.failed(view, "The stream ended.")
            finally:
                close()

        response = Response(
            body(), content_type=f"multipart/x-mixed-replace; boundary={match.group(1)}"
        )
        # A response can close before its body runs: release the slot then too.
        response.call_on_close(close)
        response.headers["X-Accel-Buffering"] = "no"
        return response

    def snapshot(view):
        _view(view)
        if view not in MJPEG:
            raise ServiceError(404, "This view has no JPEG snapshot.")
        relay.redeem(view, "snapshot")
        try:
            body = sources().frame(view)
        except SourceError as error:
            relay.failed(view, str(error))
            raise ServiceError(502, str(error)) from None
        relay.saw_frame(view)
        name = f"{view}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jpg"
        return Response(
            body,
            mimetype="image/jpeg",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    # These two routes accept a ticket instead of the operator token.
    stream.console_ticket = True
    snapshot.console_ticket = True
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
