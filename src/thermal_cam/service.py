"""Single-owner acquisition service. USB is accessed only by its worker thread."""

from __future__ import annotations

import io
import json
import logging
import queue
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import numpy as np

from labkit.describe import describe
from labkit.events import EventLog
from labkit.lease import Lease

from .camera import P1Camera
from .roi import DEFAULT_ROI_PATH, load_regions

MAX_FRAME_AGE_S = 3


@dataclass(frozen=True)
class ThermalFrame:
    sequence: int
    timestamp: str
    monotonic_time: float
    source: str
    ir: np.ndarray
    raw: np.ndarray
    temperatures_c: np.ndarray
    stats: dict

    @classmethod
    def from_arrays(cls, ir, raw, sequence: int, source: str = "camera", *, clock=None):
        if raw.shape != (120, 160) or ir.shape != raw.shape:
            raise ValueError("Expected native P1 arrays with shape (120, 160)")
        if raw.dtype != np.uint16 or ir.dtype != np.uint8:
            raise ValueError("Expected raw uint16 and IR uint8 arrays")
        if np.all(raw == 0) or np.all(raw == 65535):
            raise ValueError("Camera returned an invalid thermal frame")
        raw, ir = raw.copy(), ir.copy()
        temperatures = raw.astype(np.float64) / 64.0 - 273.15

        def point(flat_index):
            y, x = np.unravel_index(flat_index, temperatures.shape)
            return {"celsius": float(temperatures[y, x]), "x": int(x), "y": int(y)}

        stats = {
            "min": point(np.argmin(temperatures)),
            "max": point(np.argmax(temperatures)),
            "center": point(60 * 160 + 80),
            "mean_celsius": float(np.mean(temperatures)),
            "stddev_celsius": float(np.std(temperatures)),
            "range_celsius": float(np.ptp(temperatures)),
        }
        for array in (raw, ir, temperatures):
            array.flags.writeable = False
        return cls(
            sequence,
            datetime.now(timezone.utc).isoformat(),
            (clock or time.monotonic)(),
            source,
            ir,
            raw,
            temperatures,
            stats,
        )

    def metadata(self):
        return {
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "source": self.source,
            "width": 160,
            "height": 120,
            "temperature_unit": "celsius",
            "raw_unit": "1/64 kelvin",
            "stats": self.stats,
        }

    def payload(self):
        return {
            **self.metadata(),
            "temperatures_c": self.temperatures_c.tolist(),
            "raw": self.raw.tolist(),
            "ir": self.ir.tolist(),
        }

    def npz(self):
        buffer = io.BytesIO()
        np.savez_compressed(
            buffer,
            raw=self.raw,
            ir=self.ir,
            temperatures_c=self.temperatures_c,
            metadata=json.dumps(self.metadata()),
        )
        return buffer.getvalue()


class DemoCamera:
    """Explicit synthetic source for developing the GUI without USB hardware."""

    def open(self):
        self.started = time.monotonic()
        self.gain = "high"
        return {"model": "P1 simulation", "firmware": "synthetic", "width": 160, "height": 120}

    def read(self):
        time.sleep(0.04)
        y, x = np.mgrid[:120, :160]
        t = time.monotonic() - self.started
        temperatures = (
            22
            + x / 160 * 4
            + 15 * np.exp(-((x - 80 - 25 * np.sin(t / 3)) ** 2 + (y - 55) ** 2) / 500)
        )
        raw = np.rint((temperatures + 273.15) * 64).astype(np.uint16)
        ir = ((temperatures - temperatures.min()) / np.ptp(temperatures) * 255).astype(np.uint8)
        return ir, raw

    def close(self):
        pass

    def shutter(self):
        time.sleep(0.1)

    def set_gain(self, gain):
        self.gain = gain

    def stats(self):
        return {"gain": self.gain}


class CameraService:
    def __init__(
        self,
        *,
        demo=False,
        camera_factory: Callable | None = None,
        roi_path=DEFAULT_ROI_PATH,
        clock=None,
    ):
        self.source = "demo" if demo else "camera"
        self.clock = clock or (lambda: time.monotonic())
        self.regions = load_regions(roi_path)
        # The CLI's Uvicorn configuration sends INFO records from this logger
        # to stderr, which the service journal captures. No logging is configured
        # at module import, and embedded applications keep control of logging.
        self.events = EventLog(logger=logging.getLogger("uvicorn.error"))
        self.lease = Lease(clock=self.clock, on_expire=self.on_lease_expired)
        self._state_seq = 0
        self._factory = camera_factory or (DemoCamera if demo else P1Camera)
        self._lock = threading.RLock()
        self._lifecycle = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._commands = queue.Queue()
        self._frame = None
        self._state = "disconnected"
        self._error = None
        self._device = {}
        self._frames = 0
        self._fps = 0.0
        self._camera_stats = {}

    def _event(self, level, message):
        self.events.add(level, message, service="thermal", source=self.source)

    def describe(self):
        return {
            **describe(
                service="thermal",
                version="0.1.0",
                demo=self.source == "demo",
                zones=[],
                lease_seconds=self.lease.seconds,
                on_lease_expiry="continue_acquisition",
                commands={},
            ),
            "regions": {name: region.bounds() for name, region in self.regions.items()},
            "max_frame_age_s": MAX_FRAME_AGE_S,
        }

    def snapshot(self):
        # Lease callbacks must run before the service lock is taken.
        self.lease.poll()
        lease = self.lease.snapshot()
        with self._lock:
            status = self.status()
            self._state_seq += 1
            fault = None
            if self._error:
                fault = {"code": "camera_error", "message": self._error}
            elif self._state == "streaming":
                if self._frame is None:
                    fault = {"code": "frame_unavailable", "message": "No camera frame available"}
                elif status["frame_age_s"] > MAX_FRAME_AGE_S:
                    fault = {"code": "frame_stale", "message": "Camera frame is stale"}
            return {
                **status,
                "service": "thermal",
                "seq": self._state_seq,
                "ts_unix": time.time(),
                "demo": self.source == "demo",
                "connected": self._state == "streaming",
                "busy": False,
                "locked": False,
                "fault": fault,
                "lease": lease,
                "last_command": None,
                "frame_sequence": self._frame.sequence if self._frame else None,
            }

    def stop(self, client, wait_s):
        self._event("info", "Contract stop is a no-op. Acquisition continues.")
        return "confirmed"

    def on_lease_expired(self):
        self._event("info", "Thermal lease expired. Acquisition continues.")

    def region_reading(self, region, *, name=None):
        with self._lock:
            frame = self.latest()
            values = frame.temperatures_c[region.y0 : region.y1, region.x0 : region.x1]
            result = {
                "name": name,
                **region.bounds(),
                "min_c": float(np.min(values)),
                "mean_c": float(np.mean(values)),
                "max_c": float(np.max(values)),
                "frame_sequence": frame.sequence,
                "timestamp": frame.timestamp,
                "source": frame.source,
                "demo": self.source == "demo",
            }
            age = self.clock() - frame.monotonic_time
            if age > MAX_FRAME_AGE_S:
                raise RuntimeError("Camera frame is stale; reconnect the camera")
            return {**result, "frame_age_s": age}

    def connect(self):
        with self._lifecycle, self._lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            self._stop.clear()
            self._frame = None
            self._state, self._error = "connecting", None
            self._device, self._camera_stats = {}, {}
            self._fps = 0.0
            self._event("info", "Camera connection requested.")
            self._thread = threading.Thread(target=self._run, name="p1-acquisition", daemon=True)
            self._thread.start()
            return self.status()

    def disconnect(self):
        with self._lifecycle:
            was_connected = self._state != "disconnected"
            self._stop.set()
            thread = self._thread
            if thread:
                thread.join(timeout=15)
                if thread.is_alive():
                    raise RuntimeError(
                        "Camera is still stopping; wait and retry before reconnecting"
                    )
            with self._lock:
                self._state, self._error = "disconnected", None
                self._frame = None
                self._fps = 0.0
                if was_connected:
                    self._event("info", "Camera disconnected.")
            return self.status()

    def status(self):
        with self._lock:
            age = self.clock() - self._frame.monotonic_time if self._frame else None
            return {
                "state": self._state,
                "source": self.source,
                "error": self._error,
                "device": dict(self._device),
                "frames": self._frames,
                "fps": round(self._fps, 1),
                "frame_age_s": age,
                "camera_stats": dict(self._camera_stats),
            }

    def latest(self):
        with self._lock:
            if self._state != "streaming" or self._frame is None:
                raise RuntimeError(self._error or "No camera frame available")
            if self.clock() - self._frame.monotonic_time > MAX_FRAME_AGE_S:
                raise RuntimeError("Camera frame is stale; reconnect the camera")
            return self._frame

    def command(self, name, argument=None):
        with self._lock:
            if self._state != "streaming" or self._stop.is_set():
                raise RuntimeError("Camera is not streaming")
            future = Future()
            self._commands.put((name, argument, future))
        try:
            return future.result(timeout=12)
        except FutureTimeout:
            future.cancel()
            raise RuntimeError("Camera command timed out; check camera status") from None

    def _run(self):
        camera = None
        try:
            camera = self._factory()
            device = camera.open()
            with self._lock:
                self._device = device
            previous = self.clock()
            consecutive_bad = 0
            while not self._stop.is_set():
                self.lease.poll()
                while not self._commands.empty():
                    name, argument, future = self._commands.get_nowait()
                    if not future.set_running_or_notify_cancel():
                        continue
                    try:
                        # A command changes the sensor state. Do not publish a
                        # pre-command frame while shutter/gain settling runs.
                        with self._lock:
                            self._frame = None
                            self._fps = 0.0
                        if name == "shutter":
                            camera.shutter()
                        elif name == "gain":
                            camera.set_gain(argument)
                        else:
                            raise ValueError("Unknown camera command")
                        previous = self.clock()
                        self._event("info", f"Camera {name} completed.")
                        future.set_result({"ok": True, "command": name})
                    except Exception as exc:
                        future.set_exception(exc)
                        raise
                if self._stop.is_set():
                    break
                ir, raw = camera.read()
                try:
                    frame = ThermalFrame.from_arrays(
                        ir, raw, self._frames + 1, self.source, clock=self.clock
                    )
                except ValueError:
                    consecutive_bad += 1
                    if consecutive_bad >= 25:
                        raise RuntimeError("Camera returned 25 consecutive invalid frames")
                    continue
                consecutive_bad = 0
                now = frame.monotonic_time
                instant_fps = 1 / max(now - previous, 0.001)
                previous = now
                with self._lock:
                    self._fps = (
                        instant_fps if not self._fps else self._fps * 0.9 + instant_fps * 0.1
                    )
                    self._frames += 1
                    self._frame = frame
                    if self._state != "streaming":
                        self._event("info", "Camera is streaming.")
                    self._state = "streaming"
                    self._camera_stats = camera.stats()
        except Exception as exc:
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
                self._state = "error"
                self._frame = None
                self._fps = 0.0
                self._event("error", self._error)
        finally:
            try:
                if camera is not None:
                    camera.close()
            except Exception as exc:
                with self._lock:
                    self._error = self._error or f"Camera cleanup: {exc}"
                    self._event("error", f"Camera cleanup: {exc}")
            with self._lock:
                if self._stop.is_set():
                    self._state = "disconnected"
                    self._frame = None
                while not self._commands.empty():
                    _, _, future = self._commands.get_nowait()
                    if not future.done():
                        future.set_exception(RuntimeError(self._error or "Camera stopped"))
