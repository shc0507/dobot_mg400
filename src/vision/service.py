"""Serialized vision inference with contract leases and explicit startup."""

import copy
import importlib
import io
import json
import math
import re
import threading
import time
from pathlib import Path

import numpy as np
from flask import Flask, Response, jsonify, request
from PIL import Image

from labkit.describe import command, describe
from labkit.errors import ServiceError
from labkit.events import EventLog
from labkit.flask import contract_blueprint
from labkit.lease import Lease
from vision.frames import DemoSource, FrameError, Go2RTCSource, validate_camera
from vision.localizer import Detection, Localizer, draw_detections
from vision.qr import DetectionCancelled, detect_qr_codes

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/vision"
DEFAULT_WEIGHTS = "/var/lib/mg400/models/chip-arm-yolo26s-seg-v1.pt"
MAX_RESULT_BYTES = 256 * 1024


class DemoLocalizer:
    """Use fixture detections without importing a detector runtime."""

    def __init__(self, directory=FIXTURES):
        self.data = json.loads((Path(directory) / "detections.json").read_text())

    def predict(self, image):
        detections = [Detection(**item) for item in self.data["detections"]]
        return draw_detections(image, detections), {
            "image_size": {"width": image.width, "height": image.height},
            "coordinate_system": "oriented_original_image_pixels",
            "counts": dict(self.data["counts"]),
            "detections": copy.deepcopy(self.data["detections"]),
        }


class VisionService:
    def __init__(
        self,
        *,
        demo=False,
        frames=None,
        weights=DEFAULT_WEIGHTS,
        device=None,
        gpu_fraction=0.15,
        fixture_dir=FIXTURES,
        localizer_factory=None,
        torch_loader=None,
        qr_detector=detect_qr_codes,
        clock=time.monotonic,
        wall_clock=time.time,
    ):
        if (
            isinstance(gpu_fraction, bool)
            or not isinstance(gpu_fraction, (int, float))
            or not math.isfinite(gpu_fraction)
            or not 0 < gpu_fraction <= 1
        ):
            raise ValueError("GPU fraction must be finite and greater than 0, up to 1.")
        if device is not None and (
            not isinstance(device, str) or not re.fullmatch(r"cpu|cuda(?::[0-9]+)?", device)
        ):
            raise ValueError("Device must be cpu, cuda, or cuda:N.")
        self.demo = demo
        self.frames = (
            frames if frames is not None else (DemoSource(fixture_dir) if demo else Go2RTCSource())
        )
        self.weights, self.device, self.gpu_fraction = weights, device, gpu_fraction
        self.fixture_dir = Path(fixture_dir)
        self._factory = localizer_factory
        self._torch_loader = torch_loader or (lambda: importlib.import_module("torch"))
        self._qr = qr_detector
        self._wall_clock = wall_clock
        self._lock = threading.RLock()
        self._inference = threading.Lock()
        self._quit = threading.Event()
        self._startup_cancel = threading.Event()
        self._startup_done = threading.Event()
        self._startup_done.set()
        self._poller = self._worker = None
        self._model = None
        self._started = self._starting = self._closed = False
        self._connected = False
        self._active = None
        self._next = self._seq = self.expiry_count = 0
        self._fault = self._last_command = self._summary = None
        self.events = EventLog(clock=wall_clock)
        self.lease = Lease(3, clock, on_expire=self.on_lease_expired)

    def describe(self):
        camera = {"type": "integer", "minimum": 1, "maximum": 3}
        params = {
            "type": "object",
            "properties": {"camera": camera},
            "required": ["camera"],
            "additionalProperties": False,
        }
        labels = copy.deepcopy(params)
        labels["properties"]["region"] = {
            "type": "array",
            "items": {"type": "integer", "minimum": 0},
            "minItems": 4,
            "maxItems": 4,
            "description": "Pixel crop [x0, y0, x1, y1], with exclusive upper bounds.",
        }
        return describe(
            service="vision",
            version="1.0",
            demo=self.demo,
            zones=[],
            on_lease_expiry="cancel the controlling client's inference and discard late results",
            commands={"detect": command(params), "read_labels": command(labels)},
        )

    def snapshot(self):
        lease = self.lease.snapshot()
        with self._lock:
            return copy.deepcopy(
                {
                    "service": "vision",
                    "seq": self._seq,
                    "ts_unix": self._wall_clock(),
                    "demo": self.demo,
                    "connected": self._connected and not self._closed,
                    "busy": self._active is not None or self._starting,
                    "locked": False,
                    "fault": self._fault,
                    "lease": lease,
                    "last_command": self._last_command,
                    "model_loaded": self._model is not None and self._started,
                    "device": self.device,
                    "last_result": self._summary,
                    "calibrated_cameras": [],
                }
            )

    def start(self):
        """Load once, warm up on a synthetic image, then start lease polling."""
        with self._lock:
            if self._closed:
                raise RuntimeError("The vision service is closed.")
            if self._started:
                return
            if self._starting:
                raise RuntimeError("Vision startup is already in progress.")
            self._starting = True
            self._quit.clear()
            self._startup_cancel.clear()
            self._startup_done.clear()
        try:
            with self._inference:
                self._check_startup()
                if self.demo:
                    self.device = "demo"
                    model = self._factory() if self._factory else DemoLocalizer(self.fixture_dir)
                else:
                    torch = self._torch_loader()
                    selected = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
                    if selected.startswith("cuda"):
                        if not torch.cuda.is_available():
                            raise ValueError("CUDA was requested but is unavailable.")
                        index = int(selected.split(":", 1)[1]) if ":" in selected else 0
                        torch.cuda.set_per_process_memory_fraction(self.gpu_fraction, device=index)
                    self.device = selected
                    model = (self._factory or Localizer)(self.weights, device=selected)
                self._check_startup()
                model.predict(Image.new("RGB", (640, 640)))
            self._check_startup()
            poller = threading.Thread(target=self._poll, name="vision-lease", daemon=True)
            # Publish only after start succeeds. close() never joins an
            # unstarted thread, including a signal during Thread.start().
            poller.start()
            with self._lock:
                self._poller = poller
                self._check_startup()
                self._model = model
                self._started = True
                self._connected = self.demo
                self._fault = None
                self._seq += 1
            self.events.add("info", "vision started", demo=self.demo, device=self.device)
        except BaseException:
            with self._lock:
                self._closed = True
            self._quit.set()
            raise
        finally:
            with self._lock:
                self._starting = False
                self._startup_done.set()

    def _check_startup(self):
        if self._closed or self._startup_cancel.is_set():
            raise RuntimeError("Vision startup was cancelled.")

    def _poll(self):
        while not self._quit.wait(0.1):
            self.lease.poll()

    @staticmethod
    def _arguments(action, args):
        allowed = {"camera", "region"} if action == "read_labels" else {"camera"}
        if (
            action not in {"detect", "read_labels"}
            or not isinstance(args, dict)
            or set(args) - allowed
            or "camera" not in args
        ):
            raise ValueError("Expected detect(camera) or read_labels(camera, optional region).")
        camera = validate_camera(args["camera"])
        region = args.get("region")
        if "region" in args and (
            not isinstance(region, (list, tuple))
            or len(region) != 4
            or any(type(value) is not int or value < 0 for value in region)
            or region[2] <= region[0]
            or region[3] <= region[1]
        ):
            raise ValueError(
                "Region must be [x0, y0, x1, y1] with increasing nonnegative integers."
            )
        return camera, region

    def _reserve(self, action, args, client):
        camera, region = self._arguments(action, args)
        with self._lock:
            if not self._started or self._starting or self._closed:
                raise ServiceError(503, "Vision is not started.")
            if self._active is not None:
                raise ServiceError(409, "Vision is busy. Only one inference can run at a time.")
            self._next += 1
            job = {
                "id": str(self._next),
                "action": action,
                "camera": camera,
                "region": region,
                "client": client,
                "cancel": threading.Event(),
                "done": threading.Event(),
            }
            self._active = job
            self._seq += 1
            return job

    def command(self, action, args, client):
        job = self._reserve(action, args, client)
        try:
            worker = threading.Thread(
                target=self._execute, args=(job,), name="vision-inference", daemon=True
            )
            worker.start()
            with self._lock:
                self._worker = worker
        except Exception as exc:
            error = ServiceError(503, "Cannot start the inference worker.")
            self._finish(job, None, None, "failed", error)
            raise error from exc
        return job["id"]

    def read(self, action, camera, *, annotated=False):
        self.lease.poll()
        job = self._reserve(action, {"camera": camera}, None)
        self._execute(job)
        if job["error"] is not None:
            raise job["error"]
        if annotated:
            encoded = io.BytesIO()
            job["image"].save(encoded, format="JPEG")
            return encoded.getvalue()
        return job["result"]

    def _process(self, job):
        if job["cancel"].is_set():
            raise DetectionCancelled()
        image = self.frames.read(job["camera"])
        if job["cancel"].is_set():
            raise DetectionCancelled()
        if job["action"] == "detect":
            annotated, result = self._model.predict(image)
            result = copy.deepcopy(result)
        else:
            region = job["region"] or [0, 0, image.width, image.height]
            x0, y0, x1, y1 = region
            if x1 > image.width or y1 > image.height:
                raise ValueError("Region extends beyond the frame.")
            view = np.asarray(image.crop(tuple(region)))[:, :, ::-1].copy()
            codes = self._qr(view, cancelled=job["cancel"].is_set)
            result = {
                "image_size": {"width": image.width, "height": image.height},
                "coordinate_system": "oriented_original_image_pixels",
                "region_px": region,
                "labels": [
                    {
                        "text": code.text,
                        "format": code.format,
                        "decoded": code.decoded,
                        "source": code.source,
                        "corners_px": [[x + x0, y + y0] for x, y in code.corners],
                        "center_px": [code.center[0] + x0, code.center[1] + y0],
                        "bounds_px": [
                            code.bounds[0] + x0,
                            code.bounds[1] + y0,
                            code.bounds[2] + x0,
                            code.bounds[3] + y0,
                        ],
                    }
                    for code in codes
                ],
            }
            annotated = image
        if job["cancel"].is_set():
            raise DetectionCancelled()
        result.update(camera=job["camera"], calibrated=False)
        return self._bounded_result(result), annotated

    @staticmethod
    def _bounded_result(result):
        chunks, size = [], 0
        try:
            encoder = json.JSONEncoder(ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            for chunk in encoder.iterencode(result):
                size += len(chunk)
                if size > MAX_RESULT_BYTES:
                    raise ServiceError(503, "Vision result exceeds the 256 KiB JSON limit.")
                chunks.append(chunk)
            return json.loads("".join(chunks))
        except (TypeError, ValueError) as exc:
            raise ServiceError(503, "Vision result is not finite JSON data.") from exc

    def _execute(self, job):
        result = image = error = None
        outcome = "ok"
        try:
            with self._inference:
                result, image = self._process(job)
        except DetectionCancelled:
            outcome, error = (
                "stopped",
                ServiceError(409, "Inference stopped; its result was discarded."),
            )
        except FrameError as exc:
            outcome, error = "failed", ServiceError(503, str(exc))
        except ServiceError as exc:
            outcome, error = "failed", exc
        except ValueError as exc:
            outcome, error = "failed", ServiceError(400, str(exc))
        except Exception as exc:
            outcome, error = "failed", ServiceError(503, f"Vision processing failed: {exc}")
        self._finish(job, result, image, outcome, error)

    def _finish(self, job, result, image, outcome, error):
        with self._lock:
            # Stop can arrive after processing finishes but before publication.
            if job["cancel"].is_set():
                outcome, error = (
                    "stopped",
                    ServiceError(409, "Inference stopped; its result was discarded."),
                )
            if outcome == "ok":
                self._summary = {
                    "camera": job["camera"],
                    "action": job["action"],
                    "count": len(result.get("detections", result.get("labels", []))),
                    "finished_at": self._wall_clock(),
                }
                self._connected = True
                self._fault = None
            elif outcome == "failed":
                self._fault = {"code": "vision_failed", "message": error.message}
                if error.status == 503:
                    self._connected = False
            if job["client"] is not None:
                self._last_command = {key: job[key] for key in ("id", "action", "client")}
                self._last_command.update(
                    result=outcome,
                    message="Inference complete." if error is None else error.message,
                    finished_at=self._wall_clock(),
                    output=copy.deepcopy(result) if outcome == "ok" else None,
                )
            job.update(
                result=result if outcome == "ok" else None,
                image=image if outcome == "ok" else None,
                error=error,
            )
            self._active = None
            self._seq += 1
            self.events.add(
                "info" if outcome != "failed" else "error",
                "vision inference finished",
                action=job["action"],
                camera=job["camera"],
                result=outcome,
            )
            job["done"].set()

    def stop(self, client, wait_s=0):
        waiting = []
        with self._lock:
            if self._starting:
                self._startup_cancel.set()
                waiting.append(self._startup_done)
            job = self._active
            if job is not None:
                job["cancel"].set()
                waiting.append(job["done"])
            if waiting:
                self._seq += 1
        deadline = time.monotonic() + wait_s
        return (
            "confirmed"
            if all(done.wait(max(0, deadline - time.monotonic())) for done in waiting)
            else "pending"
        )

    def on_lease_expired(self):
        with self._lock:
            self.expiry_count += 1
            if self._active is not None and self._active["client"] is not None:
                self._active["cancel"].set()
                self._seq += 1
        self.events.add("warning", "vision control lease expired")

    def close(self, wait_s=15):
        with self._lock:
            self._closed = True
        self._quit.set()
        outcome = self.stop("shutdown", wait_s)
        with self._lock:
            poller = self._poller
        if poller is not None and poller is not threading.current_thread():
            poller.join(timeout=1)
        return outcome


def create_app(service, *, token=None, base_path="/"):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 4096
    blueprint = contract_blueprint(service, token=token, base_path=base_path, lease=service.lease)

    def camera():
        values = request.args.getlist("camera")
        if set(request.args) != {"camera"} or len(values) != 1 or values[0] not in {"1", "2", "3"}:
            raise ValueError("Specify camera=1, camera=2, or camera=3 exactly once.")
        return int(values[0])

    @blueprint.get("/api/detect")
    def detect():
        return jsonify(service.read("detect", camera()))

    @blueprint.get("/api/labels")
    def labels():
        return jsonify(service.read("read_labels", camera()))

    @blueprint.get("/api/annotated.jpg")
    def annotated():
        return Response(service.read("detect", camera(), annotated=True), mimetype="image/jpeg")

    app.register_blueprint(blueprint)
    return app
