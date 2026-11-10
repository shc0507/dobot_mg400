"""Console-owned stage calibration, bounded frame processing and profile storage."""

from __future__ import annotations

import base64
import copy
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

from cameras.config import camera_configuration
from vision.stage_twin import (
    AXIS_ENDPOINTS,
    ENDPOINTS,
    MAX_PROFILE_BYTES,
    StageConverter,
    TuningPolicy,
    analyze_attempt,
    evaluate_profile,
    new_profile,
    read_profile_json,
    utc_now,
)
from vision.stage_twin.marker_registry import MARKER_GROUPS

PROFILE_ROOT = Path("/var/lib/mg400/calibration/twin")
CAMERA_LABELS = ("Stage", "Insert rail", "Microscope side", "Tray")


class CameraFrames:
    """Read configured optical streams without opening or reconfiguring devices."""

    def __init__(self, settings, *, demo=False):
        self.demo = demo
        self.configuration = camera_configuration({} if demo else settings)

    def cameras(self):
        streams = (
            ("camera1", "camera2", "camera3", "camera4")
            if self.demo
            else self.configuration.streams
        )
        return [{"id": name, "label": CAMERA_LABELS[int(name[-1]) - 1]} for name in streams]

    def identity(self, camera):
        if not isinstance(camera, str) or camera not in {item["id"] for item in self.cameras()}:
            raise ValueError("Choose a configured Console camera.")
        return "demo:" + camera if self.demo else self.configuration.devices[int(camera[-1]) - 1]

    def read(self, camera):
        self.identity(camera)
        if self.demo:
            import cv2
            import numpy as np

            # A fixed offline scene supports detector/UI checks, not actuator simulation.
            pixels = np.full((480, 640), 255, dtype=np.uint8)
            dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
            for marker, x, y in ((10, 40, 40), (14, 180, 160), (18, 320, 160), (22, 460, 300)):
                pixels[y : y + 60, x : x + 60] = cv2.aruco.generateImageMarker(
                    dictionary, marker, 60
                )
            ok, encoded = cv2.imencode(".jpg", pixels)
            if not ok:
                raise RuntimeError("Cannot encode the demo camera frame.")
            return encoded.tobytes()
        # Decode through the existing bounded reader. The returned JPEG is also
        # sent to the browser, so overlays always use the pixels we processed.
        image = self._source().still(int(camera[-1]))
        return self._jpeg(image)

    def _source(self, timeout=2):
        from vision.frames import Go2RTCSource

        return Go2RTCSource(
            timeout=timeout,
            settings={
                "MG400_CAMERA_DEVICES": ",".join(self.configuration.devices),
                "MG400_CAMERA_MODES": ",".join(
                    f"{w}x{h}@{fps}" for w, h, fps in self.configuration.modes
                ),
            },
        )

    @staticmethod
    def _jpeg(image):
        output = BytesIO()
        image.save(output, format="JPEG", quality=95)
        return output.getvalue()

    def sample(self, camera, dimensions=None):
        """Use the calibration's native pixels, or 4K for a new live calibration."""
        self.identity(camera)
        if self.demo:
            return self.read(camera)
        dimensions = dimensions or (3840, 2160)
        number = int(camera[-1])
        if tuple(dimensions) == self.configuration.mode(number)[:2]:
            return self.read(camera)
        # A mode change includes exposure settling and preview restoration.
        image = self._source(timeout=30).full_still(number, dimensions)
        return self._jpeg(image)


def _identifier(value):
    if not isinstance(value, str):
        raise ValueError("Choose a saved calibration profile.")
    try:
        identifier = str(UUID(value))
    except ValueError as exc:
        raise ValueError("Choose a saved calibration profile.") from exc
    if identifier != value:
        raise ValueError("Choose a saved calibration profile.")
    return identifier


class ProfileStore:
    """Fixed directory, immutable profile snapshots and atomic selection writes."""

    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()

    def _write(self, path, value):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
        if len(data) > MAX_PROFILE_BYTES:
            raise ValueError("Calibration profile is too large.")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, suffix=".tmp", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o640)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def load(self, identifier):
        path = self.root / "profiles" / (_identifier(identifier) + ".json")
        with path.open("rb") as stream:
            return evaluate_profile(read_profile_json(stream.read(MAX_PROFILE_BYTES + 1)))[
                "profile"
            ]

    def catalog(self, identity):
        entries = []
        for path in (self.root / "profiles").glob("*.json"):
            try:
                profile = self.load(path.stem)
            except (ValueError, OSError):
                continue
            if profile["camera"]["device_id"] == identity:
                entries.append(
                    {key: profile[key] for key in ("id", "name", "updated_at", "camera")}
                )
        return sorted(entries, key=lambda item: item["updated_at"], reverse=True)

    def selections(self):
        try:
            with (self.root / "selection.json").open("rb") as stream:
                data = stream.read(8193)
        except FileNotFoundError:
            return {}
        if len(data) > 8192:
            raise ValueError("Saved camera selections are invalid.")
        value = json.loads(data)
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            raise ValueError("Saved camera selections are invalid.")
        return {key: _identifier(identifier) for key, identifier in value.items()}

    def select(self, identity, identifier):
        with self.lock:
            selections = self.selections()
            selections[identity] = _identifier(identifier)
            self._write(self.root / "selection.json", selections)

    def save(self, profile):
        with self.lock:
            snapshot = copy.deepcopy(profile)
            snapshot.update(id=str(uuid4()), updated_at=utc_now())
            snapshot = evaluate_profile(snapshot)["profile"]
            path = self.root / "profiles" / (snapshot["id"] + ".json")
            self._write(path, snapshot)
            try:
                self.select(snapshot["camera"]["device_id"], snapshot["id"])
            except (OSError, ValueError):
                path.unlink(missing_ok=True)
                raise
            return snapshot

    def delete(self, identifier, identity):
        with self.lock:
            profile = self.load(identifier)
            if profile["camera"]["device_id"] != identity:
                raise ValueError("This calibration profile belongs to another camera.")
            selections = self.selections()
            remaining = {key: value for key, value in selections.items() if value != identifier}
            if remaining != selections:
                self._write(self.root / "selection.json", remaining)
            try:
                (self.root / "profiles" / (_identifier(identifier) + ".json")).unlink()
            except OSError:
                if remaining != selections:
                    self._write(self.root / "selection.json", selections)
                raise

    def contains(self, identifier):
        with self.lock:
            return (self.root / "profiles" / (_identifier(identifier) + ".json")).is_file()


@dataclass
class Page:
    camera: str | None
    profile: dict
    active_profile: dict | None = None
    selected: str | None = None
    warning: str | None = None
    touched: float = field(default_factory=time.monotonic)
    lock: object = field(default_factory=threading.RLock)
    policy: object = None

    def reset(self):
        self.policy = TuningPolicy(self.active_profile["axes"])


class TwinConsole:
    """Page-local drafts share the profile catalog and a single processing slot."""

    def __init__(self, root, frames):
        self.store = ProfileStore(root)
        self.frames = frames
        self.pages = {}
        self.lock = threading.RLock()
        self.detector = threading.Lock()

    @contextmanager
    def processing(self):
        if not self.detector.acquire(blocking=False):
            raise RuntimeError("Digital Twin is processing a frame. Try again.")
        try:
            yield
        finally:
            self.detector.release()

    def _blank(self, camera, name=""):
        if not isinstance(name, str):
            raise ValueError("Profile name must be text.")
        profile = new_profile(name=name)
        if camera:
            profile["camera"].update(
                device_id=self.frames.identity(camera),
                label=next(item["label"] for item in self.frames.cameras() if item["id"] == camera),
            )
        return profile

    def _source(self, page, camera):
        profile = self._blank(camera)
        selected, warning = None, None
        if camera:
            try:
                selected = self.store.selections().get(self.frames.identity(camera))
                if selected:
                    profile = self.store.load(selected)
                    if profile["camera"]["device_id"] != self.frames.identity(camera):
                        raise ValueError("The saved profile belongs to another camera.")
            except (ValueError, OSError) as error:
                selected = None
                profile = self._blank(camera)
                warning = f"The saved profile is unavailable: {error}"
        page.camera = camera
        page.profile = copy.deepcopy(profile)
        page.active_profile = copy.deepcopy(profile)
        page.selected, page.warning = selected, warning
        page.reset()

    def page(self, client):
        with self.lock:
            if client not in self.pages:
                now = time.monotonic()
                for key, page in list(self.pages.items()):
                    if now - page.touched > 1800 and page.lock.acquire(blocking=False):
                        try:
                            del self.pages[key]
                        finally:
                            page.lock.release()
                if len(self.pages) >= 64:
                    raise RuntimeError(
                        "Too many Digital Twin pages are open. Close an unused page."
                    )
                cameras = self.frames.cameras()
                camera = "camera1" if any(item["id"] == "camera1" for item in cameras) else None
                page = Page(camera, self._blank(camera))
                self._source(page, camera)
                self.pages[client] = page
            page = self.pages[client]
            page.touched = time.monotonic()
            return page

    def state(self, page):
        self.sync_selection(page)
        return {
            "cameras": self.frames.cameras(),
            "camera": page.camera,
            "profiles": self.store.catalog(self.frames.identity(page.camera))
            if page.camera
            else [],
            "selected_profile": page.selected,
            "active_profile": copy.deepcopy(page.active_profile),
            "calibration": evaluate_profile(page.profile),
            "warning": page.warning,
        }

    def sync_selection(self, page):
        # Check only this page under its lock. Never acquire another page's lock.
        if page.selected and not self.store.contains(page.selected):
            page.selected = None
            page.active_profile = self._blank(page.camera)
            page.reset()

    def _activate(self, page, profile):
        page.profile = copy.deepcopy(profile)
        page.active_profile = copy.deepcopy(profile)
        page.selected = profile["id"]
        page.reset()

    def mutate(self, page, action, data):
        self.sync_selection(page)
        if action == "source":
            self.frames.identity(data.get("camera"))
            self._source(page, data["camera"])
        elif action == "profile/new":
            page.profile = self._blank(page.camera, data.get("name", ""))
        elif action == "profile/select":
            with self.store.lock:
                profile = self.store.load(data.get("id"))
                if not page.camera or profile["camera"]["device_id"] != self.frames.identity(
                    page.camera
                ):
                    raise ValueError("This calibration profile belongs to another camera.")
                self.store.select(self.frames.identity(page.camera), profile["id"])
                self._activate(page, profile)
        elif action == "profile/upload":
            if not page.camera:
                raise ValueError("Choose a configured Console camera before uploading.")
            payload = data.get("profile")
            if not isinstance(payload, str):
                raise ValueError("Upload a calibration profile JSON file.")
            try:
                payload = payload.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("The calibration profile must contain valid UTF-8 text.") from exc
            profile = evaluate_profile(read_profile_json(payload))["profile"]
            camera = self._blank(page.camera)["camera"]
            profile["camera"].update(device_id=camera["device_id"], label=camera["label"])
            self._activate(page, self.store.save(profile))
        elif action == "profile/delete":
            if not page.camera:
                raise ValueError("Choose a configured Console camera before deleting.")
            self.store.delete(data.get("id"), self.frames.identity(page.camera))
        else:
            profile = copy.deepcopy(page.profile)
            if action == "clear":
                endpoint = data.get("endpoint")
                if not isinstance(endpoint, str) or endpoint not in ENDPOINTS:
                    raise ValueError("Choose a calibration endpoint.")
                profile["endpoints"].pop(endpoint, None)
                if not profile["endpoints"]:
                    profile["camera"]["dimensions"] = None
            else:
                for key in ("name", "travel_mm"):
                    if key in data:
                        profile[key] = data[key]
            profile["updated_at"] = utc_now()
            profile = evaluate_profile(profile)["profile"]
            if action == "profile/save":
                if not page.camera:
                    raise ValueError("Choose a configured Console camera before saving.")
                profile = self.store.save(profile)
                page.selected = profile["id"]
                page.active_profile = copy.deepcopy(profile)
                page.reset()
            page.profile = profile
        page.warning = None
        return self.state(page)

    def read(self, camera, profile=None):
        data = (
            self.frames.read(camera)
            if profile is None
            else self.frames.sample(camera, profile["camera"]["dimensions"])
        )
        return data, utc_now()

    @staticmethod
    def image(data):
        return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")

    def measure(self, page, data):
        self.sync_selection(page)
        if not page.camera:
            raise ValueError("No Console camera is configured.")
        automatic, limit = data.get("automatic", True), data.get("max_frames", 4)
        if type(data.get("tune", False)) is not bool:
            raise ValueError("Tune must be true or false.")
        page.policy.configure(automatic=automatic, max_frames=limit)
        if data.get("tune", False):
            page.policy.start_manual()
        with self.processing():
            frame, received_at = self.read(page.camera, page.active_profile)
            report = StageConverter.from_profile(page.active_profile).measure(
                frame, preset=page.policy.next_preset()
            )
            page.policy.observe(report)
        return {
            **report,
            "image": self.image(frame),
            "camera": page.camera,
            "profile_id": page.active_profile["id"],
            "received_at": received_at,
            "tuning": page.policy.snapshot(),
        }

    def capture(self, page, data):
        self.sync_selection(page)
        endpoint, automatic, limit = (
            data.get("endpoint"),
            data.get("automatic", True),
            data.get("max_frames", 8),
        )
        if not isinstance(endpoint, str) or endpoint not in ENDPOINTS:
            raise ValueError("Choose a calibration endpoint.")
        if type(automatic) is not bool or type(limit) is not int or not 1 <= limit <= 8:
            raise ValueError("Choose automatic tuning and a frame limit from 1 to 8.")
        if not page.camera:
            raise ValueError("No Console camera is configured.")
        axis = ENDPOINTS[endpoint]["axis"]
        opposite = next(key for key in AXIS_ENDPOINTS[axis] if key != endpoint)
        best = None
        started = time.monotonic()
        with self.processing():
            for attempt in range(1, (limit if automatic else 1) + 1):
                frame, received_at = self.read(page.camera, page.profile)
                result = analyze_attempt(frame, endpoint, attempt, page.profile["marker_groups"])
                dimensions = page.profile["camera"]["dimensions"]
                if dimensions and result["capture"]["image_size_px"] != dimensions:
                    raise ValueError(
                        "Camera resolution differs from this profile. Start a new profile."
                    )
                tuning = result["tuning"]
                profile = copy.deepcopy(page.profile)
                profile["endpoints"][endpoint] = result["capture"]
                profile["camera"]["dimensions"] = result["capture"]["image_size_px"]
                profile["updated_at"] = utc_now()
                profile = evaluate_profile(profile)["profile"]
                fits = profile["axis_status"][axis]["status"] == "calibrated"
                rank = (
                    0
                    if not tuning["usable"]
                    else 2
                    if opposite not in page.profile["endpoints"] or fits
                    else 1
                )
                score = (rank, len(tuning["found"]), len(result["capture"]["detections"]))
                if best is None or score > best[0]:
                    best = (score, result, frame, received_at, profile)
                if rank == 2 and tuning["complete"] or time.monotonic() - started >= 20:
                    break
        _, result, frame, received_at, profile = best
        tuning = result["tuning"]
        previous = page.profile["endpoints"].get(endpoint)
        previous_ids = {marker["id"] for marker in previous["detections"]} if previous else set()
        groups = page.profile["marker_groups"]
        previous_usable = bool(previous_ids.intersection(groups[axis])) and bool(
            previous_ids.intersection(groups[MARKER_GROUPS[axis].parent])
        )
        accepted = bool(result["capture"]["detections"])
        if not accepted:
            tuning["reason"] = "No decoded markers are available."
        elif not tuning["usable"] and previous_usable:
            accepted = False
        elif (
            page.profile["axis_status"][axis]["status"] == "calibrated"
            and profile["axis_status"][axis]["status"] != "calibrated"
        ):
            accepted = False
            tuning["reason"] = profile["axis_status"][axis]["message"]
        elif tuning["usable"] and profile["axis_status"][axis]["status"] == "error":
            tuning["reason"] = profile["axis_status"][axis]["message"]
        if not accepted and previous:
            tuning["reason"] = (
                tuning["reason"] or "The capture is unusable."
            ) + " Previous capture kept."
        if accepted:
            page.profile = profile
        return {
            "state": self.state(page),
            "accepted": accepted,
            "sample": {**result["capture"], "image": self.image(frame), "received_at": received_at},
            "tuning": {**result["tuning"], "tested": attempt},
        }
