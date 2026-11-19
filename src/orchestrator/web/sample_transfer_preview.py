"""Coupled motor and optical-camera simulation for the native local preview."""

import copy
import threading
import time
from uuid import uuid4

from labkit.errors import ServiceError

# This model is used only for the optional legacy optical demonstration.
MICROSTEP = 8
NOMINAL_PULSES_PER_MM = 640

AXES = {"top": "x", "bottom": "y", "insert": "insert"}
TRAVEL = {"x": 45.0, "y": 40.0, "insert": 150.0}
SIMULATION_SPEED = 100


class PreviewStage:
    """Advance synthetic motion from elapsed time, without serial or network access."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.position = {axis: travel / 2 for axis, travel in TRAVEL.items()}
        self.pulses = {motor: 0 for motor in AXES}
        self.operation = None
        self.last_command = None
        self.stop_epoch = 0
        self.microstep = 2

    def _advance(self):
        operation = self.operation
        if operation is None:
            return
        elapsed = max(0, self.clock() - operation["started"])
        fraction = min(1.0, elapsed / operation["duration"])
        axis, motor = AXES[operation["motor"]], operation["motor"]
        self.position[axis] = operation["start_mm"] + operation["delta_mm"] * fraction
        self.pulses[motor] = operation["start_pulses"] + round(operation["delta_pulses"] * fraction)
        if fraction == 1:
            self.last_command = {
                "id": operation["id"],
                "result": "ok",
                "message": "Preview move completed.",
            }
            self.operation = None

    def coordinates(self):
        with self.lock:
            self._advance()
            return dict(self.position)

    def state(self):
        with self.lock:
            self._advance()
            moving = self.operation["motor"] if self.operation else None
            return {
                "connected": True,
                "feedback_fresh": True,
                "locked": False,
                "demo": True,
                "busy": moving is not None,
                "stop_epoch": self.stop_epoch,
                "microstep": self.microstep,
                "port": "preview-stage",
                "last_command": copy.deepcopy(self.last_command),
                "motors": [
                    {
                        "id": motor,
                        "mode": "moving" if moving == motor else "idle",
                        "position": self.pulses[motor],
                        "available": moving != motor,
                        "cooldown": 0,
                        "acceleration": 50,
                    }
                    for motor in AXES
                ],
            }

    def move(self, *, motor, direction, speed, steps, stop_epoch):
        with self.lock:
            self._advance()
            if stop_epoch != self.stop_epoch:
                raise ServiceError(409, "The preview motion was cancelled by Stop.")
            if self.operation is not None:
                raise ServiceError(409, "Wait for the preview stage to stop.")
            if motor not in AXES or direction not in ("cw", "ccw"):
                raise ValueError("Choose a preview motor and direction.")
            if (
                type(steps) is not int
                or not 1 <= steps <= 100000
                or type(speed) is not int
                or not 1 <= speed <= 1000
            ):
                raise ValueError("Use valid stage pulses and speed.")
            axis = AXES[motor]
            scale = NOMINAL_PULSES_PER_MM * self.microstep / MICROSTEP
            delta = steps / scale * (1 if direction == "ccw" else -1)
            if not 0 <= self.position[axis] + delta <= TRAVEL[axis]:
                raise ValueError("The preview move exceeds the calibrated stage travel.")
            identifier = uuid4().hex
            self.operation = {
                "id": identifier,
                "motor": motor,
                "started": self.clock(),
                "duration": max(0.03, steps / speed / SIMULATION_SPEED),
                "start_mm": self.position[axis],
                "delta_mm": delta,
                "start_pulses": self.pulses[motor],
                "delta_pulses": steps if direction == "cw" else -steps,
            }
            return {"outcome": "accepted", "command_id": identifier}

    def prepare(self, stop_epoch):
        with self.lock:
            self._advance()
            if stop_epoch != self.stop_epoch:
                raise ServiceError(409, "The preview motion was cancelled by Stop.")
            if self.operation is not None:
                raise ServiceError(409, "Wait for the preview stage to stop.")
            self.microstep = MICROSTEP
            identifier = uuid4().hex
            self.last_command = {"id": identifier, "result": "ok"}
            return {"outcome": "accepted", "command_id": identifier}

    def stop(self):
        with self.lock:
            self._advance()
            self.stop_epoch += 1
            if self.operation is not None:
                self.last_command = {
                    "id": self.operation["id"],
                    "result": "stopped",
                    "message": "Preview stage stopped.",
                }
                self.operation = None
            return {"outcome": "confirmed"}


class PreviewStageFrames:
    """Draw markers that follow the same Mock stage calibration as Position Finder."""

    def __init__(self, stage):
        from .twin import CameraFrames

        self.frames = CameraFrames({}, demo=True)
        self.stage = stage

    def cameras(self):
        return self.frames.cameras()

    def identity(self, camera):
        return self.frames.identity(camera)

    def read(self, camera):
        return self._render(camera, 6)

    def sample(self, camera, dimensions=None):
        # Preserve old local profiles. New mock profiles use enough pixels for fine moves.
        scale = dimensions[0] / 640 if dimensions else 6
        return self._render(camera, scale)

    def _render(self, camera, scale):
        self.identity(camera)
        if camera != "camera1":
            return self.frames.read(camera)
        import cv2
        import numpy as np

        coordinates = self.stage.coordinates()
        y = 115 + 90 * coordinates["y"] / TRAVEL["y"]
        x = 275 + 90 * coordinates["x"] / TRAVEL["x"]
        insert = x + 95 + 90 * coordinates["insert"] / TRAVEL["insert"]
        pixels = np.full((round(480 * scale), round(640 * scale)), 255, dtype=np.uint8)
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        for marker, left, top in ((10, 40, 40), (14, 180, y), (18, x, y), (22, insert, y + 140)):
            left, top = round(left * scale), round(top * scale)
            size = round(60 * scale)
            pixels[top : top + size, left : left + size] = cv2.aruco.generateImageMarker(
                dictionary, marker, size
            )
        ok, encoded = cv2.imencode(".jpg", pixels)
        if not ok:
            raise RuntimeError("Cannot encode the preview camera frame.")
        return encoded.tobytes()
