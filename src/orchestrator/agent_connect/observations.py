"""One read-only supplement cache for thermal statistics and existing vision results."""

import copy
import math
import re
import threading
import time
from itertools import islice
from urllib.parse import urlencode

from labkit.errors import ServiceError

INTERVAL_S = 1.0
MAX_REGIONS = 32
MAX_ITEMS = 50
THERMAL_MAX_AGE_S = 3.0


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _text(value, limit=256):
    return value[:limit] if isinstance(value, str) else None


def _vector(value, size=2):
    if isinstance(value, (list, tuple)) and len(value) == size:
        result = [_number(item) for item in value]
        if None not in result:
            return result
    return None


def _missing(reason, *, instance=None):
    return {
        "available": False,
        "quality": "unavailable",
        "reason": reason,
        "instance": instance,
        "observed_at": None,
        "cache_age_s": None,
    }


def thermal_summary(response):
    """Keep scalar statistics from one frame. Never keep thermal matrices."""
    stats = response.get("stats") or {}
    compact = {
        key: _number(stats.get(key)) for key in ("mean_celsius", "stddev_celsius", "range_celsius")
    }
    for key in ("min", "max", "center"):
        point = stats.get(key) or {}
        compact[key] = {field: _number(point.get(field)) for field in ("celsius", "x", "y")}
    regions = response.get("regions") or {}
    selected = {}
    for name, region in islice(regions.items(), MAX_REGIONS):
        if not isinstance(name, str) or not isinstance(region, dict):
            continue
        selected[name[:64]] = {
            field: _number(region.get(field))
            for field in ("x0", "y0", "x1", "y1", "min_c", "mean_c", "max_c")
        }
    return {
        "available": True,
        "quality": "valid",
        "reason": None,
        "instance": _text(response.get("instance"), 128),
        "frame_sequence": _number(response.get("sequence")),
        "source": _text(response.get("source")),
        "temperature_unit": "celsius",
        "width": _number(response.get("width")),
        "height": _number(response.get("height")),
        "source_timestamp": _text(response.get("timestamp")),
        "timestamp_basis": _text(response.get("timestamp_basis")),
        "capture_timestamp": _text(response.get("capture_timestamp"))
        if isinstance(response.get("capture_timestamp"), str)
        else _number(response.get("capture_timestamp")),
        "frame_age_s": _number(response.get("frame_age_s")),
        "stats": compact,
        "regions": selected,
        "count": len(selected),
        "total": len(regions),
        "complete": len(selected) == len(regions),
    }


def vision_summary(response):
    """Extract bounded points and labels, without retaining images or polygons."""
    output = response.get("output") or {}
    available = response.get("available") is True
    value = {
        "available": available,
        "quality": "unknown" if available and response.get("status") == "ok" else "unavailable",
        "reason": _text(response.get("reason")),
        "instance": _text(response.get("instance"), 128),
        "job_id": _text(response.get("job_id"), 20),
        "action": _text(response.get("action")),
        "camera": _number(response.get("camera")),
        "status": _text(response.get("status")),
        "finished_at": _number(response.get("finished_at")),
        "source_observed_at": _number(response.get("observed_at")),
        "source_identity_known": bool(response.get("instance")),
        "result_source": _text(response.get("result_source", "retained_result")),
        "read_at": _number(output.get("read_at")),
        "coordinate_system": _text(output.get("coordinate_system")),
        "calibrated": output.get("calibrated") if type(output.get("calibrated")) is bool else None,
        "items": [],
        "count": 0,
        "total": 0,
        "complete": True,
    }
    if not available or response.get("status") != "ok":
        return value
    collections = [(kind, output.get(kind) or []) for kind in ("detections", "labels", "markers")]
    slots = output.get("slots") or {}
    if isinstance(slots, dict):
        collections.append(
            ("slots", [{"id": key, **row} for key, row in slots.items() if isinstance(row, dict)])
        )
    for kind, rows in collections:
        if not isinstance(rows, list):
            continue
        value["total"] += len(rows)
        for row in rows[: MAX_ITEMS - len(value["items"])]:
            if not isinstance(row, dict):
                continue
            item = {"kind": kind}
            for key in ("id", "class_id"):
                if type(row.get(key)) is int or isinstance(row.get(key), str):
                    item[key] = _text(row[key]) if isinstance(row[key], str) else row[key]
            for key in ("label", "text", "format", "state"):
                if isinstance(row.get(key), str):
                    item[key] = _text(row[key])
                    if len(row[key]) > 256:
                        item["text_truncated"] = True
            for key in ("confidence", "angle_deg"):
                if key in row:
                    item[key] = _number(row[key])
            if "decoded" in row and type(row["decoded"]) is bool:
                item["decoded"] = row["decoded"]
            point = _vector(row.get("center_px", row.get("center_xy")))
            if point is not None:
                item["center_px"] = point
            if (point := _vector(row.get("center_xy_mm"))) is not None:
                item["center_xy_mm"] = point
            robot = row.get("robot_mm")
            if isinstance(robot, dict):
                item["robot_mm"] = {axis: _number(robot.get(axis)) for axis in ("x", "y")}
            value["items"].append(item)
    value["count"] = len(value["items"])
    value["complete"] = value["count"] == value["total"]
    return value


class SharedObservations:
    """Own the supplement readers. snapshot() performs no network or device reads."""

    def __init__(self, service, *, clock=time.monotonic, wall_clock=time.time):
        self.service, self.clock, self.wall_clock = service, clock, wall_clock
        self.lock = threading.Lock()
        self.refresh_lock = threading.Lock()
        self.stopping = threading.Event()
        self.worker = None
        self.next_refresh = 0
        self.adapters = {}
        self.vision_key = None
        self.rows = {name: (_missing("not_read"), None) for name in ("thermal", "vision")}

    def start(self):
        with self.lock:
            if self.worker is None and not self.stopping.is_set():
                self.worker = threading.Thread(
                    target=self._run, name="agent-observations", daemon=True
                )
                self.worker.start()
        return self

    def _run(self):
        while not self.stopping.is_set():
            self.refresh()
            self.stopping.wait(INTERVAL_S)

    def close(self):
        self.stopping.set()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(5)
        # A request may still be finishing. Its refresh owns and closes its transport.
        if self.refresh_lock.acquire(blocking=False):
            try:
                for name in tuple(self.adapters):
                    self._close_adapter(name)
            finally:
                self.refresh_lock.release()

    def _close_adapter(self, name):
        adapter = self.adapters.pop(name, None)
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                pass

    def _store(self, name, value):
        with self.lock:
            self.rows[name] = (value, self.clock())

    def refresh(self):
        """Read at most once per second, independently of the subscriber count."""
        if self.stopping.is_set() or not self.refresh_lock.acquire(blocking=False):
            return
        try:
            if self.clock() < self.next_refresh:
                return
            self.next_refresh = self.clock() + INTERVAL_S
            for name in ("thermal", "vision"):
                if self.stopping.is_set():
                    break
                try:
                    self._read(name)
                except Exception:
                    self._close_adapter(name)
                    cached = self.service.devices.get(name)
                    self._store(
                        name,
                        _missing(
                            "read_failed", instance=(cached.get("state") or {}).get("instance")
                        ),
                    )
            if self.stopping.is_set():
                for name in tuple(self.adapters):
                    self._close_adapter(name)
        finally:
            self.refresh_lock.release()

    def _read(self, name):
        cached = self.service.devices.get(name)
        state = cached.get("state") or {}
        instance = state.get("instance")
        if cached.get("phase") != "ready" or not self.service.dashboard_configured(name):
            self._close_adapter(name)
            self._store(name, _missing("source_unavailable", instance=instance))
            if name == "vision":
                self.vision_key = None
            return
        key = self._vision_identity(state) if name == "vision" else None
        if name == "vision" and key is None:
            self._store(name, _missing("no_result", instance=instance))
            self.vision_key = None
            return
        if name == "vision" and instance is not None and key == self.vision_key:
            return
        if name == "vision" and instance is None:
            inline = self._inline_vision(state, key)
            if inline is not None:
                value = vision_summary(inline)
                value.update(observed_at=self.wall_clock(), request_elapsed_s=0)
                self._store(name, value)
                self.vision_key = key
                return
        if name == "vision" and key == self.vision_key:
            return
        adapter = self.adapters.get(name)
        if adapter is None:
            adapter = self.service.console_client(name, "agent-observations-" + name)
            if adapter is None:
                self._store(name, _missing("source_unavailable", instance=instance))
                return
            self.adapters[name] = adapter
        path = (
            "api/frame?summary=1"
            if name == "thermal"
            else (
                "api/results/" + key[1] + ("?" + urlencode({"instance": key[0]}) if key[0] else "")
            )
        )
        started = self.clock()
        try:
            response = adapter.request(path)
        except ServiceError as error:
            if name != "vision" or (
                error.status != 410 and not (instance is None and error.status in (400, 404))
            ):
                raise
            # The shared HTTP client raises for expired or evicted results.
            # Keep this identity unavailable until a new job or process appears.
            self._store(
                name,
                {
                    **_missing(
                        "source_identity_unavailable"
                        if instance is None
                        else "result_expired_or_unavailable",
                        instance=instance,
                    ),
                    "job_id": key[1],
                },
            )
            self.vision_key = key
            return
        elapsed = max(0, self.clock() - started)
        if instance is not None and response.get("instance") != instance:
            self._store(name, _missing("instance_changed", instance=response.get("instance")))
            return
        if name == "vision" and str(response.get("job_id")) != key[1]:
            self._store(name, _missing("result_mismatch", instance=instance))
            return
        if name == "vision" and instance is None:
            # A response alone cannot establish continuity with an unidentified cache.
            response = {**response, "instance": None, "result_source": "unidentified_result"}
        value = thermal_summary(response) if name == "thermal" else vision_summary(response)
        value.update(observed_at=self.wall_clock(), request_elapsed_s=elapsed)
        if name == "thermal" and value["frame_age_s"] is not None:
            # The full request duration gives a conservative age at receipt.
            value["frame_age_s"] += elapsed
        self._store(name, value)
        if name == "vision" and response.get("status") != "pending":
            self.vision_key = key

    @staticmethod
    def _vision_identity(state):
        instance = state.get("instance")
        if instance is not None and (
            not isinstance(instance, str) or not re.fullmatch(r"[0-9a-f]{32}", instance)
        ):
            return None
        candidates = {}
        for key, field in (("last_result", "job_id"), ("last_command", "id")):
            row = state.get(key) or {}
            job = row.get(field)
            if (
                instance is not None and row.get("instance", instance) != instance
            ) or not isinstance(job, str):
                continue
            if re.fullmatch(r"[1-9][0-9]{0,19}", job):
                candidates[job] = _number(row.get("finished_at"))
        if not candidates:
            return None
        job = max(candidates, key=int)
        return instance, job, candidates[job]

    @staticmethod
    def _inline_vision(state, key):
        """Use the latest cached result when process identity is not available."""
        for name, field in (("last_command", "id"), ("last_result", "job_id")):
            row = state.get(name) or {}
            if row.get(field) != key[1]:
                continue
            status = row.get("status", row.get("result"))
            output = row.get("output")
            if name == "last_command" and status in ("running", "pending"):
                return {
                    "instance": None,
                    "job_id": key[1],
                    "available": False,
                    "status": "pending",
                    "reason": "pending",
                    "result_source": "console_cache",
                }
            if not isinstance(output, dict) and status not in ("failed", "rejected", "stopped"):
                continue
            return {
                **row,
                "instance": None,
                "job_id": key[1],
                "available": True,
                "status": status or "ok",
                "result_source": "console_cache",
                "camera": row.get("camera", (output or {}).get("camera")),
            }
        return None

    def snapshot(self):
        with self.lock:
            stored = copy.deepcopy(self.rows)
        result = {}
        now = self.clock()
        for name, (row, received) in stored.items():
            age = None if received is None else max(0, now - received)
            row["cache_age_s"] = age
            if name == "thermal" and row.get("available"):
                frame_age = row.get("frame_age_s")
                row["frame_age_s"] = None if frame_age is None else frame_age + age
                row["quality"] = (
                    "unknown"
                    if frame_age is None
                    else ("stale" if row["frame_age_s"] > THERMAL_MAX_AGE_S else "valid")
                )
            result[name] = row
        return result
