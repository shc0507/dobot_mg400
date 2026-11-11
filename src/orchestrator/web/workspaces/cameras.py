"""Console workspace: Cameras.

The browser loads camera1 to camera4 and the microscope from the proxy at
``/camera/<view>.mp4``, ``.mjpeg`` and the still URLs. The console relays only data:

- thermal: the thermal frame API through the thermal service client;
- camera 4 chip detections: the vision ``api/detect`` result.
"""

import base64
import json
import math
import secrets
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from flask import jsonify, request

from cameras.config import camera_configuration
from labkit import ports
from labkit.errors import ServiceError

from . import Workspace

CAMERAS = ("camera1", "camera2", "camera3", "camera4")
VIEWS = (*CAMERAS, "thermal", "microscope")
# The browser loads these views from the camera preview behind the proxy.
PROXIED = (*CAMERAS, "microscope")
STALE_S = 2.0
RECENT_S = 5.0
# The tile polls every 40 ms. Viewers within 40 ms share 1 upstream read.
THERMAL_CACHE_S = 0.04
DETECTION_CACHE_S = 1.0
# The camera preview reports the MJPEG frames per second that each viewer of a proxied
# view gets. H.264 tiles count their frames in the browser.
PREVIEW_FPS_URL = ports.url("camera", "fps.json")
PREVIEW_FPS_CACHE_S = 0.5
MAX_JSON = 4 * 1024 * 1024
UPSTREAM_TIMEOUT_S = 5.0


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
        return {view: payload[view] for view in PROXIED if _finite(payload.get(view))}

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


class Relay:
    """Per-view frame times of the relayed thermal view."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.frame_at = {}
        self.error = {}

    def saw_frame(self, view, at=None):
        with self.lock:
            self.frame_at[view] = self.clock() if at is None else at
            self.error.pop(view, None)

    def failed(self, view, message):
        with self.lock:
            self.error[view] = (message, self.clock())

    def view_state(self, view):
        now = self.clock()
        with self.lock:
            seen = self.frame_at.get(view)
            error = self.error.get(view)
        age = None if seen is None else max(0.0, now - seen)
        if error is not None and (seen is None or error[1] >= seen):
            state = "offline"
        elif age is None or age > RECENT_S:
            state = "offline"
        else:
            state = "live" if age <= STALE_S else "stale"
        return {
            "state": state,
            "age_s": None if age is None else round(age, 1),
            "error": None if error is None else error[0],
        }


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
            if view in PROXIED:
                # The browser loads these streams from the proxy; it shows their state.
                row["url"] = f"/camera/{view}.mjpeg"
                row["fps"] = preview_fps.get(view)
            else:
                row.update(relay.view_state(view))
            row["mode"] = _mode_text(modes.get(view))
            views.append(row)
        return jsonify(views=views)

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
