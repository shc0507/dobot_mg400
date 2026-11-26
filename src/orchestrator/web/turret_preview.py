"""Local turret calibration preview with synthetic frames and no device access."""

import copy
import json
import os
import tempfile
import threading
from pathlib import Path

from labkit.errors import ServiceError


class TurretPreview:
    """Exercise the production profile format and image measurement on this PC."""

    def __init__(self, root):
        from microscope.turret_calibration import TurretProfileStore

        self.store = TurretProfileStore(root)
        self.lock = threading.RLock()
        self.draft = None
        self.session = None
        self.objective = 0
        self.travel_limit_path = Path(root).parent / "focus-travel-limit.json"
        self.autofocus_settings_path = Path(root).parent / "autofocus-settings.json"

    def autofocus_settings(self, patch=None):
        """Keep mock search settings separate from the manual movement budget."""
        from microscope.focus_travel import (
            AUTOFOCUS_RANGE_FIELDS,
            DEFAULT_AUTOFOCUS_RANGES,
            MAX_HALF_RANGE,
        )

        with self.lock:
            try:
                saved = json.loads(self.autofocus_settings_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                saved = {}
            values = DEFAULT_AUTOFOCUS_RANGES | saved
            if patch is not None:
                self._idle()
                if (
                    not isinstance(patch, dict)
                    or not patch
                    or set(patch) - set(AUTOFOCUS_RANGE_FIELDS)
                ):
                    raise ValueError("Provide at least one known lens autofocus half-range.")
                if any(
                    type(value) is not int or not 4 <= value <= MAX_HALF_RANGE
                    for value in patch.values()
                ):
                    raise ValueError(
                        f"Search half-ranges must be from 4 to {MAX_HALF_RANGE} pulses."
                    )
                values |= patch
                self.autofocus_settings_path.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary = tempfile.mkstemp(
                    prefix=".autofocus-", dir=self.autofocus_settings_path.parent
                )
                try:
                    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                        json.dump(values, output)
                    os.replace(temporary, self.autofocus_settings_path)
                finally:
                    Path(temporary).unlink(missing_ok=True)
            return {
                **values,
                "minimum_half_range_pulses": 4,
                "maximum_half_range_pulses": MAX_HALF_RANGE,
            }

    def travel_limit(self, pulses=None):
        """Keep the local preview setting without opening a device connection."""
        from microscope.focus_travel import DEFAULT_FOCUS_TRAVEL

        with self.lock:
            if pulses is not None:
                self._idle()
                if type(pulses) is not int or not 1 <= pulses <= 2147483647:
                    raise ValueError("Max focus travel must be a positive integer.")
                self.travel_limit_path.parent.mkdir(parents=True, exist_ok=True)
                self.travel_limit_path.write_text(json.dumps({"pulses": pulses}), encoding="utf-8")
            try:
                value = json.loads(self.travel_limit_path.read_text(encoding="utf-8"))["pulses"]
            except FileNotFoundError:
                value = DEFAULT_FOCUS_TRAVEL
            return {"focus_travel_limit_pulses": value}

    def state(self, client):
        from microscope.turret_calibration import visual_ready

        with self.lock:
            selected = self.store.selection_status()["profile_id"]
            profiles = [{**item, "id": item["profile_id"]} for item in self.store.list()]
            return {
                "demo": True,
                "has_control": True,
                "session": copy.deepcopy(self.session),
                "draft": copy.deepcopy(self.draft),
                "profiles": profiles,
                "selected_profile": selected,
                "camera": {"stream": "camera3", "dimensions": [1280, 720]},
                "alignment": "image_coordinates",
                "dictionary": "DICT_4X4_50",
                "switch_focus": 0,
                "compatible_for_switch": bool(
                    self.draft and visual_ready(self.draft) and self.draft["camera"] == "camera3"
                ),
            }

    def _owned(self, client):
        if not self.session or self.session["client"] != client:
            raise ServiceError(409, "Start your calibration session first.")

    def _idle(self):
        if self.session is not None:
            raise ServiceError(409, "Finish calibration before changing saved profiles.")

    def action(self, client, action, data):
        from microscope.turret_calibration import (
            new_focus_profile,
            stable_capture,
            upgrade_focus_profile,
            validate_profile,
        )

        with self.lock:
            if action == "begin":
                if self.session and self.session["client"] != client:
                    raise ServiceError(409, "Another page is calibrating the turret.")
                if data.get("new") or self.draft is None:
                    self.draft = new_focus_profile(
                        data.get("name") or "Turret calibration", "camera3"
                    )
                if self.draft["schema_version"] != 5:
                    raise ValueError("Draft camera or height format changed; create a new profile.")
                self.session = {
                    "active": True,
                    "client": client,
                    "focus_position": 0,
                    "focus_zeroed": False,
                }
            elif action == "record":
                self._owned(client)
                objective = data.get("objective_id")
                if type(objective) is not int or objective not in range(4):
                    raise ValueError("Choose an objective ID from 0 to 3.")
                self.objective = objective
                frame = self.image()
                captured = stable_capture([frame.copy() for _ in range(3)], objective)
                self.draft["captures"][str(objective)] = captured
            elif action == "record_initial":
                self._owned(client)
                self.objective = 0
                frame = self.image()
                self.draft["initial_5x_capture"] = stable_capture(
                    [frame.copy() for _ in range(3)], 0
                )
            elif action in {"finish", "cancel"}:
                self._owned(client)
                self.session = None
            elif action == "save":
                self._idle()
                if self.draft is None:
                    raise ValueError("Create a turret calibration first.")
                candidate = copy.deepcopy(self.draft)
                candidate["name"] = data.get("name") or candidate["name"]
                self.draft = self.store.save(validate_profile(candidate))
            elif action == "select":
                self._idle()
                self.draft = self.store.select(data.get("id"))
            elif action == "load":
                self._idle()
                self.draft = self.store.load(data.get("id"))
                if self.draft["schema_version"] != 5:
                    self.draft = upgrade_focus_profile(self.draft)
            elif action == "delete":
                self._idle()
                self.store.delete(data.get("id"))
                if self.draft and self.draft["profile_id"] == data.get("id"):
                    self.draft = None
            elif action == "upload":
                self._idle()
                self.draft = self.store.upload(data["profile"].encode("utf-8"))
            else:
                raise ValueError("Unknown turret calibration action.")
            return self.state(client)

    def profile(self, identifier):
        return self.store.load(identifier)

    def image(self):
        import cv2
        import numpy as np

        frame = np.full((720, 1280, 3), (238, 241, 244), dtype=np.uint8)
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        cv2.ellipse(frame, (880, 260), (255, 80), 0, 0, 360, (94, 103, 116), -1)
        cv2.rectangle(frame, (785, 290), (975, 590), (193, 201, 210), -1)
        x, y, size = 850, 390 - self.objective * 10, 56
        cv2.rectangle(frame, (x - 12, y - 12), (x + size + 12, y + size + 12), (255, 255, 255), -1)
        marker = cv2.aruco.generateImageMarker(dictionary, self.objective, size)
        frame[y : y + size, x : x + size] = cv2.cvtColor(marker, cv2.COLOR_GRAY2BGR)
        labels = [
            ("LOCAL PREVIEW - SYNTHETIC CAMERA", (40, 60)),
            (f"Objective {self.objective}", (785, 645)),
        ]
        for text, position in labels:
            cv2.putText(
                frame, text, position, cv2.FONT_HERSHEY_SIMPLEX, 0.8, (70, 78, 89), 2, cv2.LINE_AA
            )
        return frame

    def frame(self):
        import cv2

        with self.lock:
            return cv2.imencode(".jpg", self.image(), [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes()
