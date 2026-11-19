"""Local turret calibration preview with synthetic frames and no device access."""

import copy
import threading

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

    def state(self, client):
        from microscope.turret_calibration import is_complete

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
                    self.draft and is_complete(self.draft) and self.draft["camera"] == "camera3"
                ),
            }

    def _owned(self, client):
        if not self.session or self.session["client"] != client:
            raise ServiceError(409, "Start your calibration session first.")

    def _idle(self):
        if self.session is not None:
            raise ServiceError(409, "Finish calibration before changing saved profiles.")

    def action(self, client, action, data):
        from microscope.turret_calibration import new_profile, stable_capture, validate_profile

        with self.lock:
            if action == "begin":
                if self.session and self.session["client"] != client:
                    raise ServiceError(409, "Another page is calibrating the turret.")
                if data.get("new") or self.draft is None:
                    self.draft = new_profile(data.get("name") or "Turret calibration", "camera3", 0)
                self.session = {
                    "active": True,
                    "client": client,
                    "focus_position": 0,
                    "focus_zeroed": True,
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
        x, y, size = 850, 390, 56
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
