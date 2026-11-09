"""Serialized vision inference with contract leases and explicit startup."""

import copy
import hashlib
import importlib
import io
import json
import math
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, Response, jsonify, request
from PIL import Image

from labkit.describe import command, describe
from labkit.errors import ServiceError
from labkit.events import EventLog
from labkit.flask import contract_blueprint
from labkit.lease import Lease
from vision.calibration import (
    DEFAULT_DIRECTORY,
    DRIFT_LIMIT_PX,
    CalibrationStore,
    calibration_settings,
    camera_device,
    detect_markers,
    drift_inputs,
    drift_proof,
    image_size,
    robot_xy,
    scan_r,
)
from vision.frames import DemoSource, FrameError, Go2RTCSource, validate_camera
from vision.localizer import Detection, Localizer, draw_detections
from vision.qr import DetectionCancelled, detect_qr_codes
from vision.tray import cross_check_slots, locate_tray, validate_tray

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/vision"
DEFAULT_WEIGHTS = "/var/lib/mg400/models/chip-arm-yolo26s-seg-v1.pt"
MAX_RESULT_BYTES = 256 * 1024
MAX_REQUEST_BYTES = 16 * 1024


def _slot_arguments(args):
    """Validate bounded data, never a server-side tray filename."""
    if set(args) != {"camera", "tray", "targets", "axis"}:
        raise ValueError("Slot observation needs camera, tray, targets and axis.")
    if args["axis"] not in ("long", "short"):
        raise ValueError("Chip axis must be explicitly long or short.")
    tray = validate_tray(args["tray"])
    targets = args["targets"]
    ids = {slot["id"] for slot in tray["slots"]}
    if not isinstance(targets, dict) or not 1 <= len(targets) <= 128:
        raise ValueError("Slot observation requires explicit taught XY targets.")
    for ident, point in targets.items():
        if ident in ids and point is None:
            continue  # Observed without a taught station: a cup pick uses the seen XY.
        if (
            ident not in ids
            or not isinstance(point, list)
            or len(point) != 2
            or any(
                type(value) not in (int, float)
                or not -1_000_000 <= value <= 1_000_000
                or not math.isfinite(value)
                for value in point
            )
        ):
            raise ValueError("Slot targets must name configured slots with finite taught XY.")
    # Match ServiceClient's complete JSON envelope and encoding, including whitespace.
    if (
        len(json.dumps({"action": "observe_slots", **args}, allow_nan=False).encode())
        > MAX_REQUEST_BYTES
    ):
        raise ValueError("Slot command exceeds the complete 16 KiB request limit.")
    return tray


def _slot_observations(image, model, location, detections, axis, requested=None):
    """Associate complete current-frame masks with visible grooves in robot XY.

    An arm box overlapping a groove, a clipped mask, or competing assignments
    prevents an empty/present assertion. Image rectangle angles are not used.
    """
    if (
        not isinstance(detections, dict)
        or detections.get("image_size") != {"width": image.width, "height": image.height}
        or detections.get("coordinate_system") != "oriented_original_image_pixels"
        or not isinstance(detections.get("detections"), list)
        or len(detections["detections"]) > 128
    ):
        raise ValueError("Incomplete current-frame detector evidence.")
    requested = set(location["slots"]) if requested is None else set(requested)
    slots, pixels = {}, {}
    for ident, geometry in location["slots"].items():
        segment = np.asarray(geometry["centerline_xy_mm"], np.float64)
        vector = segment[1] - segment[0]
        unit = vector / np.linalg.norm(vector)
        normal = np.array([-unit[1], unit[0]])
        margin = geometry["width_mm"] / 2 + location["tolerance_mm"]
        start, end = segment[0] - unit * margin, segment[1] + unit * margin
        polygon = np.array(
            [
                start + normal * margin,
                end + normal * margin,
                end - normal * margin,
                start - normal * margin,
            ]
        )
        slots[ident] = cv2.convexHull(polygon.astype(np.float32))
        if ident not in requested:
            continue
        xyz = np.column_stack((polygon, np.full(4, model["tray_height_mm"])))
        rotation = np.asarray(model["rotation"], np.float64)
        translation = np.asarray(model["translation"], np.float64)
        depth = (xyz @ cv2.Rodrigues(rotation)[0].T + translation)[:, 2]
        projected = cv2.projectPoints(
            xyz,
            rotation,
            translation,
            np.asarray(model["camera_matrix"], np.float64),
            np.asarray(model["distortion"], np.float64),
        )[0].reshape(-1, 2)
        if (
            not np.isfinite(projected).all()
            or np.any(depth <= 0)
            or any(
                not 0 < x < image.width - 1 or not 0 < y < image.height - 1 for x, y in projected
            )
        ):
            raise ValueError("A requested groove is not fully visible in the current frame.")
        pixels[ident] = cv2.convexHull(projected.astype(np.float32))
    found = {ident: [] for ident in requested}
    counts = {}
    for item in detections["detections"]:
        if not isinstance(item, dict) or item.get("label") not in ("Chip", "Robotic Arm"):
            raise ValueError("Unknown detector class in slot evidence.")
        label = item["label"]
        counts[label] = counts.get(label, 0) + 1
        confidence = item.get("confidence")
        if type(confidence) not in (int, float) or not 0 < confidence <= 1:
            raise ValueError("Invalid detector confidence.")
        if label == "Robotic Arm":
            box = item.get("xyxy")
            if (
                not isinstance(box, (list, tuple))
                or len(box) != 4
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in box)
                or not (
                    0 <= box[0] < box[2] <= image.width and 0 <= box[1] < box[3] <= image.height
                )
            ):
                raise ValueError("Incomplete arm occlusion evidence.")
            x0, y0, x1, y1 = box
            hull = np.float32([[x0, y0], [x1, y0], [x1, y1], [x0, y1]])
            if any(cv2.intersectConvexConvex(hull, groove)[0] > 0 for groove in pixels.values()):
                raise ValueError("An arm occludes a requested groove.")
            continue
        polygon = item.get("polygon")
        if (
            not isinstance(polygon, (list, tuple))
            or not 3 <= len(polygon) <= 4096
            or any(
                not isinstance(point, (list, tuple))
                or len(point) != 2
                or any(
                    type(value) not in (int, float) or not math.isfinite(value) for value in point
                )
                or not (0 < point[0] < image.width - 1 and 0 < point[1] < image.height - 1)
                for point in polygon
            )
        ):
            raise ValueError("A chip mask is missing, clipped or invalid.")
        mapped = np.float32([robot_xy(model, point, image.size) for point in polygon])
        hull = cv2.convexHull(mapped)
        if cv2.contourArea(hull) <= 1e-6:
            raise ValueError("Degenerate chip geometry.")
        matches = [
            ident
            for ident, groove in slots.items()
            if cv2.intersectConvexConvex(hull, groove)[0] > 0
        ]
        if not requested.intersection(matches):
            continue
        if len(matches) > 1:
            raise ValueError("One chip overlaps multiple configured grooves.")
        if not matches:
            continue
        center, size, angle = cv2.minAreaRect(mapped)
        if min(size) <= 1e-6 or abs(size[0] - size[1]) <= max(size) * 0.01:
            raise ValueError("Chip axis is ambiguous.")
        # OpenCV's width axis is mapped into the calibrated robot plane first.
        if (size[1] > size[0]) == (axis == "long"):
            angle += 90
        found[matches[0]].append(
            {
                "state": "present",
                "center_xy_mm": list(center),
                "angle_deg": angle % 180,
                "reason": None,
            }
        )
    if detections.get("counts") != counts:
        raise ValueError("Detector counts do not match the current frame's masks.")
    result = {}
    for ident, candidates in found.items():
        if len(candidates) > 1:
            raise ValueError("Multiple chips occupy a requested groove.")
        result[ident] = (
            candidates[0]
            if candidates
            else {"state": "empty", "center_xy_mm": None, "angle_deg": None, "reason": None}
        )
    return result


class _VisionLease(Lease):
    """Release irreversibly cancels jobs, including same-client reacquisition."""

    def __init__(self, *args, on_release, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_release = on_release

    def release(self, client):
        with self.control(client, require_holder=False):
            if self.snapshot()["holder"] == client:
                self._on_release(client)
            return super().release(client)


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
        marker_detector=detect_markers,
        calibration_directory=DEFAULT_DIRECTORY,
        calibration_options=None,
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
        self.camera_count = getattr(self.frames, "camera_count", 4)
        if type(self.camera_count) is not int or not 0 <= self.camera_count <= 4:
            raise ValueError("Frame source camera_count must be an integer from 0 through 4.")
        self.weights, self.device, self.gpu_fraction = weights, device, gpu_fraction
        self.fixture_dir = Path(fixture_dir)
        self._factory = localizer_factory
        self._torch_loader = torch_loader or (lambda: importlib.import_module("torch"))
        self._qr = qr_detector
        self._markers = marker_detector
        self.calibrations = CalibrationStore(calibration_directory)
        self._calibration_options = calibration_settings(calibration_options)
        devices = getattr(getattr(self.frames, "configuration", None), "devices", ())
        self._devices = tuple(devices) if isinstance(devices, (tuple, list)) else ()
        self._calibrated_cameras = set()
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
        self.lease = _VisionLease(
            3, clock, on_expire=self.on_lease_expired, on_release=self._released
        )

    def describe(self):
        camera = {"type": "integer", "minimum": 1, "maximum": self.camera_count}
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
        calibration = copy.deepcopy(params)
        for name, count, columns in (
            ("tag_points", 24, 3),
            ("tag_pixels", 24, 2),
            ("tray_points", 4, 3),
            ("tray_pixels", 4, 2),
        ):
            calibration["properties"][name] = {
                "type": "array",
                "minItems": count if count == 24 else 2,
                "maxItems": count,
                "items": {
                    "type": "array",
                    "minItems": columns,
                    "maxItems": columns,
                    "items": {"type": "number"},
                },
            }
            calibration["required"].append(name)
        calibration["properties"]["image_size"] = {
            "type": "array",
            "minItems": 2,
            "maxItems": 2,
            "items": {"type": "integer", "minimum": 1},
        }
        calibration["required"].append("image_size")
        calibration["properties"]["scan_r_deg"] = {
            "type": "number",
            "minimum": -1_000_000,
            "maximum": 1_000_000,
            "description": "Flange R of all 24 scan points, in degrees.",
        }
        calibration["required"].append("scan_r_deg")
        calibration["properties"]["tool_dz_mm"] = {
            "type": "number",
            "minimum": -1_000_000,
            "maximum": 1_000_000,
            "description": "Measured tool Z offset. Nominal tag-to-tip is tag height + dz.",
        }
        calibration["required"].append("tool_dz_mm")
        drift = copy.deepcopy(params)
        drift["properties"]["flange_mm"] = {
            "type": "array",
            "minItems": 4,
            "maxItems": 4,
            "items": {"type": "number", "minimum": -1_000_000, "maximum": 1_000_000},
        }
        drift["required"].append("flange_mm")
        drift["properties"]["limit_px"] = {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": DRIFT_LIMIT_PX,
            "default": DRIFT_LIMIT_PX,
            "description": "Maximum distance from the detected to the expected marker 4 center.",
        }
        slots = copy.deepcopy(params)
        slots["properties"].update(
            tray={"type": "object", "description": "Bounded tray model data, never a file path."},
            targets={
                "type": "object",
                "minProperties": 1,
                "maxProperties": 128,
                "description": "Slot ID to taught robot XY, or null for no taught station.",
            },
            axis={"type": "string", "enum": ["long", "short"]},
        )
        slots["required"] += ["tray", "targets", "axis"]
        return describe(
            service="vision",
            version="1.0",
            demo=self.demo,
            zones=[],
            on_lease_expiry="cancel the controlling client's inference and discard late results",
            commands=(
                {
                    "detect": command(params),
                    "read_labels": command(labels),
                    "read_markers": command(params),
                    "calibrate_camera": command(calibration),
                    "check_calibration": command(drift),
                    "observe_slots": command(slots),
                }
                if self.camera_count
                else {}
            ),
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
                    "calibrated_cameras": sorted(self._calibrated_cameras),
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

    def _nominal_distance(self, tool_dz_mm):
        """The tag sits above the flange and the tip below it: D = tag height + tool dz."""
        if type(tool_dz_mm) not in (int, float) or not math.isfinite(tool_dz_mm):
            raise ValueError("The tool Z offset must be a finite number.")
        distance = self._calibration_options["nominal_tag_height_mm"] + tool_dz_mm
        if not 0 < distance <= 1000:
            raise ValueError("Tag height plus tool Z offset must be from 0 to 1000 mm.")
        return float(distance)

    def _arguments(self, action, args):
        allowed = {"camera", "region"} if action == "read_labels" else {"camera"}
        if action == "calibrate_camera":
            allowed |= {
                "tag_points",
                "tag_pixels",
                "tray_points",
                "tray_pixels",
                "image_size",
                "scan_r_deg",
                "tool_dz_mm",
            }
        if action == "check_calibration":
            allowed |= {"flange_mm", "limit_px"}
        if action == "observe_slots":
            allowed |= {"tray", "targets", "axis"}
        if (
            action
            not in {
                "detect",
                "read_labels",
                "read_markers",
                "calibrate_camera",
                "check_calibration",
                "observe_slots",
            }
            or not isinstance(args, dict)
            or set(args) - allowed
            or "camera" not in args
        ):
            raise ValueError("Expected a configured vision command with its declared parameters.")
        camera = validate_camera(args["camera"], self.camera_count)
        if action == "observe_slots":
            _slot_arguments(args)
            if camera > len(self._devices):
                raise ValueError("Slot evidence requires a configured stable camera port identity.")
            camera_device(self._devices[camera - 1])
        if action == "calibrate_camera":
            if set(args) != allowed:
                raise ValueError(
                    "Calibration needs all 24 tag points, 2 to 4 tray references, the scan R "
                    "and the tool Z offset."
                )
            image_size(args["image_size"])
            scan_r(args["scan_r_deg"])
            self._nominal_distance(args["tool_dz_mm"])
            count = len(args["tray_points"]) if isinstance(args["tray_points"], list) else 0
            for name, rows, columns in (
                ("tag_points", 24, 3),
                ("tag_pixels", 24, 2),
                ("tray_points", count, 3),
                ("tray_pixels", count, 2),
            ):
                value = args[name]
                if (
                    not 2 <= count <= 4
                    or not isinstance(value, list)
                    or len(value) != rows
                    or any(not isinstance(row, list) or len(row) != columns for row in value)
                    or any(
                        type(item) not in (int, float)
                        or not math.isfinite(item)
                        or abs(item) > 1_000_000
                        for row in value
                        for item in row
                    )
                ):
                    raise ValueError("Calibration points must be bounded finite coordinate arrays.")
            if camera > len(self._devices):
                raise ValueError("Camera calibration requires a configured stable port identity.")
        if action == "check_calibration":
            if "flange_mm" not in args:
                raise ValueError("Drift checking requires a measured flange pose.")
            drift_inputs(args["flange_mm"], args.get("limit_px", DRIFT_LIMIT_PX))
            if camera > len(self._devices):
                raise ValueError(
                    "Drift checking requires a configured stable camera port identity."
                )
            camera_device(self._devices[camera - 1])
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
                "args": copy.deepcopy(args),
                "client": client,
                "cancel": threading.Event(),
                "done": threading.Event(),
            }
            self._active = job
            self._seq += 1
            return job

    def command(self, action, args, client):
        ready = threading.Event()
        job = self._reserve(action, args, client)
        accepted = False

        def execute():
            ready.wait()
            if accepted:
                self._execute(job)

        try:
            worker = threading.Thread(target=execute, name="vision-inference", daemon=True)
            worker.start()
            with self._lock:
                self._worker = worker
            accepted = True
        except BaseException as exc:
            accepted = False
            error = ServiceError(503, "Cannot start the inference worker.")
            self._finish(job, None, None, "failed", error)
            if not isinstance(exc, Exception):
                raise
            raise error from exc
        finally:
            # A start failure after thread acceptance must not admit inference
            # behind a finished reservation or a confirmed Stop response.
            ready.set()
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
        if job["action"] == "observe_slots":
            args = job["args"]
            device = self._devices[job["camera"] - 1]
            model = self.calibrations.load(device)
            if model is None:
                raise ValueError("Slot evidence requires an installed camera calibration.")
            location = locate_tray(np.asarray(image), model, args["tray"], device=device)
            if job["cancel"].is_set():
                raise DetectionCancelled()
            _, detections = self._model.predict(image)
            slots = _slot_observations(
                image, model, location, detections, args["axis"], args["targets"]
            )
            location["slots"] = {name: location["slots"][name] for name in args["targets"]}
            evidence = {
                name: {
                    "state": item["state"],
                    **(
                        {"center_xy_mm": item["center_xy_mm"]} if item["state"] == "present" else {}
                    ),
                }
                for name, item in slots.items()
            }
            cross_check_slots(location, taught_stations=args["targets"], chips=evidence)
            if job["cancel"].is_set():
                raise DetectionCancelled()
            result = {
                "camera": job["camera"],
                "camera_device": device,
                "calibrated": True,
                "model_created_at": model["created_at"],
                "image_size": {"width": image.width, "height": image.height},
                "frame_sha256": hashlib.sha256(image.tobytes()).hexdigest(),
                "read_at": self._wall_clock(),
                "axis": args["axis"],
                "slots": slots,
            }
            return self._bounded_result(result), image
        if job["action"] == "calibrate_camera":
            from vision.calibration import fit_calibration

            args = job["args"]
            if image.size != image_size(args["image_size"]):
                raise ValueError("Camera image size changed during calibration.")
            model = fit_calibration(
                args["tag_points"],
                args["tag_pixels"],
                args["tray_points"],
                args["tray_pixels"],
                args["image_size"],
                scan_r_deg=args["scan_r_deg"],
                cancelled=job["cancel"].is_set,
                **{
                    **self._calibration_options,
                    "nominal_distance_mm": self._nominal_distance(args["tool_dz_mm"]),
                },
            )
            device = self._devices[job["camera"] - 1]
            saved = self.calibrations.save(device, model, commit_guard=lambda: self._commit(job))
            with self._lock:
                self._calibrated_cameras.add(job["camera"])
            result = {"camera": job["camera"], "calibrated": True, "calibration": saved}
            return self._bounded_result(result), image
        if job["action"] == "check_calibration":
            device = self._devices[job["camera"] - 1]
            model = self.calibrations.load(device)
            if model is None:
                raise ValueError("camera moved, recalibrate: no camera calibration is installed")
            if image.size != image_size(model["image_size"]):
                raise ValueError("camera moved, recalibrate: calibrated image dimensions changed")
            if job["cancel"].is_set():
                raise DetectionCancelled()
            markers = self._markers(image)
            if job["cancel"].is_set():
                raise DetectionCancelled()
            args = job["args"]
            result = drift_proof(
                model,
                args["flange_mm"],
                markers,
                image.size,
                device=device,
                limit_px=args.get("limit_px", DRIFT_LIMIT_PX),
            )
            if job["cancel"].is_set():
                raise DetectionCancelled()
            result.update(camera=job["camera"], calibrated=True)
            # This branch never reloads the model or maps marker centers to robot coordinates.
            return self._bounded_result(result), image
        if job["action"] == "read_markers":
            result = {
                "image_size": {"width": image.width, "height": image.height},
                "coordinate_system": "oriented_original_image_pixels",
                "markers": self._markers(image),
                "frame_sha256": hashlib.sha256(image.tobytes()).hexdigest(),
                "read_at": self._wall_clock(),
                "camera_device": (
                    self._devices[job["camera"] - 1]
                    if job["camera"] <= len(self._devices)
                    else None
                ),
            }
            annotated = image
        elif job["action"] == "detect":
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
        if job["action"] != "read_markers" and job["camera"] <= len(self._devices):
            model = self.calibrations.load(self._devices[job["camera"] - 1])
            if model is not None:
                if image.size != image_size(model["image_size"]):
                    raise ValueError("Live image size differs from the stored camera calibration.")
                for item in result.get("detections", result.get("labels", [])):
                    point = item.get("center_px", item.get("center_xy"))
                    if point is None:
                        raise ValueError("Detection lacks an image-pixel center for calibration.")
                    xy = robot_xy(model, point, image.size)
                    item["robot_mm"] = {"x": xy[0], "y": xy[1]}
                result["calibrated"] = True
                with self._lock:
                    self._calibrated_cameras.add(job["camera"])
            else:
                with self._lock:
                    self._calibrated_cameras.discard(job["camera"])
        return self._bounded_result(result), annotated

    @contextmanager
    def _commit(self, job):
        # Slow solving, staging and inference precede this short authority fence.
        # Stop and lease expiry serialize with model installation or drift-result publication.
        with self.lease.control(job["client"]):
            with self._lock:
                if self._closed or self._active is not job or job["cancel"].is_set():
                    raise DetectionCancelled()
                yield

    def _released(self, client):
        with self._lock:
            if self._active is not None and self._active["client"] == client:
                self._active["cancel"].set()
                self._seq += 1

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
        if job["action"] in {"check_calibration", "observe_slots"} and outcome == "ok":
            try:
                with self._commit(job):
                    return self._finish_result(job, result, image, outcome, error)
            except (ServiceError, DetectionCancelled):
                outcome, error = (
                    "stopped",
                    ServiceError(409, "Vision check lost control; its proof was discarded."),
                )
        return self._finish_result(job, result, image, outcome, error)

    def _finish_result(self, job, result, image, outcome, error):
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
            # A detection preview polls every second, so only a failure is an event.
            if outcome == "failed":
                self.events.add(
                    "error",
                    "vision inference failed",
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


def create_app(service, *, base_path="/"):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    blueprint = contract_blueprint(service, base_path=base_path, lease=service.lease)

    def camera():
        values = request.args.getlist("camera")
        if (
            set(request.args) != {"camera"}
            or len(values) != 1
            or values[0] not in {str(number) for number in range(1, service.camera_count + 1)}
        ):
            raise ValueError("Specify one configured camera number exactly once.")
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
