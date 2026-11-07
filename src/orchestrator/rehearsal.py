"""Own isolated loopback demo services. Never load production endpoint settings."""

import copy
import errno
import ipaddress
import json
import math
import os
import runpy
import secrets
import threading
import tomllib
from contextlib import ExitStack
from pathlib import Path

from labkit.lease import validate_client
from mg400.controller_ownership import ControllerOwnership
from mg400.sim import Simulator
from mg400.web_control import RobotPanelState
from mg400.web_gripper import WebGripper

from .clients import FurnaceAdapter, RobotAdapter, ServiceAdapter, ThermalAdapter

DEMO_SERVICES = frozenset({"robot", "stage", "thermal", "furnace", "vision"})

# Synthetic calibration geometry, never hardware stations or device paths to open.
DEMO_CALIBRATION_DEVICE = "/dev/v4l/by-path/rehearsal-camera1-video-index0"
DEMO_CALIBRATION_SIZE = (1920, 1080)
DEMO_CALIBRATION_GRID = ((200.0, 0.0), (260.0, 40.0), (50.0, 90.0))
DEMO_CALIBRATION_REFERENCES = (
    (1, (160.0, -100.0, 100.0, 0.0)),
    (2, (330.0, -100.0, 100.0, 0.0)),
    (7, (160.0, 150.0, 100.0, 0.0)),
)


class _DemoCalibrationFrames:
    """Render actual ArUco pixels from this rehearsal's robot pose only."""

    camera_count = 1

    def __init__(self, robot_state_provider):
        import cv2
        import numpy as np

        from cameras.config import CameraConfig

        self.configuration = CameraConfig((DEMO_CALIBRATION_DEVICE,), ((1920, 1080, 30),))
        self._robot_state_provider = robot_state_provider
        self._lock = threading.Lock()
        self._closed = False
        self._sequence = 0
        self._matrix = np.array([[1200.0, 0, 960], [0, 1200.0, 540], [0, 0, 1]])
        self._rotation = np.array([math.pi, 0.0, 0.0])
        self._translation = np.array([-230.0, 20.0, 400.0])
        self._distortion = np.array([-0.02, 0.005, 0, 0, 0])
        self._dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        self._tray = np.full((1080, 1920), 255, dtype=np.uint8)
        for identity, (x, y, flange_z, _r) in DEMO_CALIBRATION_REFERENCES:
            self._render(self._tray, identity, (x, y, flange_z + 100 - 246))

    def _render(self, image, identity, center):
        import cv2
        import numpy as np

        # A 24 mm black square plus a 6 mm white margin on every side.
        offsets = np.array([[-18, 18, 0], [18, 18, 0], [18, -18, 0], [-18, -18, 0]])
        points = np.asarray(center, dtype=np.float64) + offsets
        camera_points = points @ cv2.Rodrigues(self._rotation)[0].T + self._translation
        if np.any(camera_points[:, 2] <= 0):
            raise ValueError("The simulated marker is behind the calibration camera.")
        projected = cv2.projectPoints(
            points, self._rotation, self._translation, self._matrix, self._distortion
        )[0].reshape(4, 2)
        if (
            not np.isfinite(projected).all()
            or np.any(projected < 0)
            or np.any(projected >= DEMO_CALIBRATION_SIZE)
        ):
            raise ValueError("The simulated marker is outside the calibration camera image.")
        marker = np.full((384, 384), 255, dtype=np.uint8)
        marker[64:320, 64:320] = cv2.aruco.generateImageMarker(self._dictionary, identity, 256)
        transform = cv2.getPerspectiveTransform(
            np.array([[0, 0], [383, 0], [383, 383], [0, 383]], dtype=np.float32),
            projected.astype(np.float32),
        )
        warped = cv2.warpPerspective(
            marker,
            transform,
            DEMO_CALIBRATION_SIZE,
            flags=cv2.INTER_LINEAR,
            borderValue=255,
        )
        np.minimum(image, warped, out=image)

    def read(self, camera):
        import numpy as np
        from PIL import Image

        from vision.frames import FrameError, validate_camera

        validate_camera(camera, self.camera_count)
        with self._lock:
            if self._closed:
                raise FrameError("The rehearsal camera source is closed.")
            robot = self._robot_state_provider()
            if robot is None:
                raise FrameError("Calibration rehearsal needs its owned robot service.")
            state = robot.snapshot()
            actual = state.get("actual")
            if (
                state.get("demo") is not True
                or state.get("feedback_fresh") is not True
                or state.get("busy") is not False
                or not isinstance(actual, (list, tuple))
                or len(actual) < 3
                or any(
                    type(value) not in (int, float) or not math.isfinite(value)
                    for value in actual[:3]
                )
            ):
                raise FrameError("The owned rehearsal robot has no stationary fresh pose.")
            image = self._tray.copy()
            self._render(image, 4, (actual[0], actual[1], actual[2] + 100))
            self._sequence += 1
            image[0, :8] = np.frombuffer(self._sequence.to_bytes(8, "big"), dtype=np.uint8)
            return Image.fromarray(image).convert("RGB")

    def close(self):
        with self._lock:
            self._closed = True


# These fixtures are an invented scene, not measured lab geometry or defaults.
DEMO_TRAY_INITIAL_FLANGE = (210.0, 10.0, 180.0, 0.0)
DEMO_TRAY_CENTERS = {"source": (220.0, 20.0), "target": (260.0, 20.0)}
DEMO_TRAY_HEIGHT = -46.0


def demo_tray_document():
    """Return an independent, explicit synthetic tray document."""
    return {
        "schema": 1,
        "name": "Owned synthetic rehearsal tray",
        "dictionary": "DICT_4X4_50",
        "origin_marker_id": 1,
        "plate_size_mm": [210.0, 290.0],
        "marker_size_mm": 24.0,
        "groove_width_mm": 8.0,
        "tolerance_mm": 2.0,
        "markers": [
            {"id": ident, "center_mm": [x, y]}
            for ident, x, y in ((1, 20, 20), (2, 190, 20), (7, 20, 270), (0, 190, 270))
        ],
        "slots": [
            {
                "id": name,
                "centerline_mm": [[x - 140, y + 100], [x - 140, y + 140]],
                "reference_mm": [x - 140, y + 120],
            }
            for name, (x, y) in DEMO_TRAY_CENTERS.items()
        ],
    }


def write_demo_tray_configuration(directory):
    """Create one private synthetic fixture directory; return settings, never apply them.

    The caller must explicitly select these settings and arrange initial robot
    readiness. No service address, credential, camera endpoint or host data is read.
    """
    from .stations import AXES, StationStore, Workspace
    from .tool import Pose

    directory = Path(directory)
    directory.mkdir(mode=0o700)

    def write(name, text):
        path = directory / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        return str(path)

    tray = demo_tray_document()
    text = "\n".join(
        f"{key} = {json.dumps(value)}"
        for key, value in tray.items()
        if key not in {"markers", "slots"}
    )
    for section, fields in (
        ("markers", ("id", "center_mm")),
        ("slots", ("id", "centerline_mm", "reference_mm")),
    ):
        for entry in tray[section]:
            text += f"\n\n[[{section}]]\n" + "\n".join(
                f"{key} = {json.dumps(entry[key])}" for key in fields
            )
    workspace = Workspace(
        {"x_mm": [140, 350], "y_mm": [-120, 170], "z_mm": [-100, 100], "r_deg": [-90, 90]}
    )
    stations = StationStore(workspace, directory / "stations.toml")
    for name, (x, y) in DEMO_TRAY_CENTERS.items():
        stations.save(name, Pose(x, y, DEMO_TRAY_HEIGHT, 0), approach_mm=20, zone="tray")
    stations.save("drift-check", Pose(210, 10, 34, 0), approach_mm=20, zone="tray")
    settings = {
        "MG400_ORCH_TRAY_FILE": write("tray.toml", text + "\n"),
        "MG400_ORCH_TRAY_CAMERA": "1",
        "MG400_ORCH_CHIP_AXIS": "long",
        "MG400_ORCH_CHIP_R_OFFSET_DEG": "0",
        "MG400_ORCH_CHIP_R_MIN_DEG": "-20",
        "MG400_ORCH_CHIP_R_MAX_DEG": "20",
        "MG400_ORCH_STATIONS_FILE": str(stations.path),
        "MG400_ORCH_TOOL_FILE": write("tool.toml", "offset_mm = [0, 0, 146]\n"),
        "MG400_ORCH_ZONES_FILE": write(
            "zones.toml", '[zones.tray]\nowner = "robot"\nconditions = []\n'
        ),
    }
    for field, axis in AXES.items():
        for index, bound in enumerate(("MIN", "MAX")):
            unit = "DEG" if axis == "R" else "MM"
            settings[f"MG400_ORCH_{axis}_{bound}_{unit}"] = str(workspace.bounds[field][index])
    return settings


class DemoTrayScene(_DemoCalibrationFrames):
    """Owned RGB tray/arm/chip pixels and matching synthetic model, with no I/O.

    Vacuum completions update this scene only at explicitly taught synthetic
    slots. The localizer measures colored chip contours in the supplied frame,
    so an old frame cannot silently obtain a later occupancy result.
    """

    def __init__(self, robot_state_provider, *, failure=None):
        if failure not in {
            None,
            "no-source",
            "occupied-target",
            "no-target",
            "source-remains",
            "moved-camera",
        }:
            raise ValueError("Unknown synthetic tray failure.")
        super().__init__(robot_state_provider)
        self._lock = threading.RLock()
        self.failure = failure
        self._occupied = {"source"} if failure != "no-source" else set()
        if failure == "occupied-target":
            self._occupied.add("target")
        self._carrying = False
        self._calibration = self._make_calibration()
        if failure == "moved-camera":
            self._translation[0] += 10
        self._tray.fill(255)
        for marker in demo_tray_document()["markers"]:
            x, y = marker["center_mm"]
            self._render(self._tray, marker["id"], (x + 140, y - 120, DEMO_TRAY_HEIGHT))

    @property
    def tray(self):
        return demo_tray_document()

    @property
    def occupancy(self):
        with self._lock:
            return {name: name in self._occupied for name in DEMO_TRAY_CENTERS}

    def _project(self, points):
        import cv2
        import numpy as np

        return cv2.projectPoints(
            np.asarray(points, dtype=np.float64),
            self._rotation,
            self._translation,
            self._matrix,
            self._distortion,
        )[0].reshape(-1, 2)

    def _make_calibration(self):
        from vision.calibration import VALIDATION_INDICES, validate_model

        points = [
            [x, y, z]
            for z in (160.0, 200.0)
            for y in (0.0, 20.0, 40.0)
            for x in (200.0, 220.0, 240.0, 260.0)
        ]
        references = [[x, y, 100.0] for _, (x, y, _z, _r) in DEMO_CALIBRATION_REFERENCES]
        training = [i for i in range(24) if i not in VALIDATION_INDICES]
        return validate_model(
            {
                "schema": 2,
                "camera_device": DEMO_CALIBRATION_DEVICE,
                "camera_matrix": self._matrix.tolist(),
                "distortion": self._distortion.tolist(),
                "rotation": self._rotation.tolist(),
                "translation": self._translation.tolist(),
                "tray_height_mm": DEMO_TRAY_HEIGHT,
                "distance_mm": 246.0,
                "nominal_tag_height_mm": 100.0,
                "nominal_distance_mm": 246.0,
                "rms_limit_mm": 1.0,
                "image_size": list(DEMO_CALIBRATION_SIZE),
                "rms_mm": {"held_out": 0.0, "tray": 0.0},
                "created_at": 1.0,
                "scan_r_deg": 0.0,
                "points": {
                    "tag_flange_mm": points,
                    "tag_pixels": self._project([[x, y, z + 100] for x, y, z in points]).tolist(),
                    "tray_tip_xy_flange_z_mm": references,
                    "tray_pixels": self._project(
                        [[x, y, DEMO_TRAY_HEIGHT] for x, y, _ in references]
                    ).tolist(),
                    "training_indices": training,
                    "validation_indices": list(VALIDATION_INDICES),
                    "inlier_indices": training,
                },
                "errors_mm": {"held_out": [0.0] * 6, "tray": [0.0] * len(references)},
            }
        )

    def calibration_model(self):
        return copy.deepcopy(self._calibration)

    def read(self, camera):
        import cv2
        import numpy as np
        from PIL import Image

        with self._lock:
            pixels = np.asarray(super().read(camera)).copy()
            robot = self._robot_state_provider()
            # Dashboard demos own one resource per service. Read the scene of
            # that same owned robot instead of creating an independent chip.
            owned_scene = getattr(robot, "_tray_scene", None)
            occupied = owned_scene.occupancy if owned_scene is not None else self.occupancy
            for name in sorted(name for name, present in occupied.items() if present):
                x, y = DEMO_TRAY_CENTERS[name]
                polygon = self._project(
                    [
                        [x + dx, y + dy, DEMO_TRAY_HEIGHT]
                        for dx, dy in ((-6, -2), (6, -2), (6, 2), (-6, 2))
                    ]
                )
                cv2.fillConvexPoly(pixels, np.rint(polygon).astype(np.int32), (200, 30, 30))
            return Image.fromarray(pixels)

    def localizer(self):
        return _DemoTrayLocalizer()

    @staticmethod
    def _slot(state):
        actual = state.get("actual") if isinstance(state, dict) else None
        if not isinstance(actual, (list, tuple)) or len(actual) < 4:
            return None
        if any(type(item) not in (int, float) or not math.isfinite(item) for item in actual[:4]):
            return None
        for name, (x, y) in DEMO_TRAY_CENTERS.items():
            if math.dist(actual[:3], (x, y, 100.0)) <= 0.1:
                return name
        return None

    def vacuum_completed(self, operation, state):
        slot = self._slot(state)
        with self._lock:
            if operation == "suck" and slot in self._occupied and not self._carrying:
                self._carrying = True
                if self.failure != "source-remains":
                    self._occupied.remove(slot)
            elif operation in {"release", "off"} and self._carrying:
                if slot is not None and self.failure != "no-target":
                    self._occupied.add(slot)
                self._carrying = False


class _DemoTrayLocalizer:
    def predict(self, image):
        import cv2
        import numpy as np

        from vision.localizer import Detection, draw_detections

        pixels = np.asarray(image)
        mask = (
            (pixels[:, :, 0] == 200) & (pixels[:, :, 1] == 30) & (pixels[:, :, 2] == 30)
        ).astype(np.uint8)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        detections = []
        for contour in contours:
            if cv2.contourArea(contour) <= 1:
                continue
            (x, y), size, angle = cv2.minAreaRect(contour)
            polygon = cv2.boxPoints(((x, y), size, angle))
            lower, upper = polygon.min(axis=0), polygon.max(axis=0)
            detections.append(
                Detection(
                    0,
                    "Chip",
                    1.0,
                    (*lower.tolist(), *upper.tolist()),
                    (float(x), float(y)),
                    tuple(map(tuple, polygon.tolist())),
                    float(angle),
                    tuple(size),
                )
            )
        from dataclasses import asdict

        return draw_detections(image, detections), {
            "image_size": {"width": image.width, "height": image.height},
            "coordinate_system": "oriented_original_image_pixels",
            "counts": {"Chip": len(detections)} if detections else {},
            "detections": [asdict(item) for item in detections],
        }


class _DemoClient:
    """A fresh adapter that cannot outlive its owned rehearsal services."""

    _METHODS = {
        "describe",
        "acquire",
        "heartbeat",
        "state",
        "owned",
        "healthy",
        "command",
        "run",
        "stop",
        "recovery_stop",
        "release",
        "roi",
        "gas_stop",
    }
    # The console reads events, relays manual commands and keyboard keys. It renews
    # only when a browser heartbeat asks, so the profile adds no renewal thread.
    _PROFILES = {"runner": frozenset(_METHODS), "console": frozenset(_METHODS | {"events", "keys"})}

    def __init__(self, owner, adapter, *, profile="runner"):
        if profile not in self._PROFILES:
            raise ValueError("Unknown rehearsal client profile.")
        self._owner, self._adapter = owner, adapter
        self._methods = self._PROFILES[profile]
        self._closed = False
        self._calls = 0
        self._close_done = threading.Event()

    @property
    def identity(self):
        return self._adapter.identity

    @property
    def timeout(self):
        return self._adapter.timeout

    def __getattr__(self, name):
        if name.startswith("_") or name not in self.__dict__.get("_methods", ()):
            raise AttributeError(name)
        operation = getattr(self._adapter, name)

        def invoke(*args, **kwargs):
            owner = self._owner
            with owner._condition:
                if not owner._active or self._closed:
                    raise RuntimeError("This rehearsal client is no longer active.")
                owner._operations += 1
                self._calls += 1
            try:
                return operation(*args, **kwargs)
            finally:
                with owner._condition:
                    owner._operations -= 1
                    self._calls -= 1
                    owner._condition.notify_all()

        return invoke

    def close(self):
        owner = self._owner
        with owner._condition:
            first = not self._closed
            if first:
                self._closed = True
                owner._operations += 1
                while self._calls:
                    owner._condition.wait()
        if not first:
            self._close_done.wait()
            return
        try:
            self._adapter.close()
        finally:
            with owner._condition:
                owner._operations -= 1
                self._close_done.set()
                owner._condition.notify_all()


class _DemoRobot(RobotPanelState):
    def __init__(self, host, lock_directory, gripper_feedback, *, tray_scene=None):
        self._tray_scene = tray_scene
        gripper = WebGripper(gripper_feedback)
        gripper.poll()
        super().__init__(host, gripper=gripper)
        self._ownership = ControllerOwnership(host, directory=lock_directory)

    def _operate(self, action, targets, speed=None):
        super()._operate(action, targets, speed)
        if action == "vacuum" and self._tray_scene is not None:
            # The snapshot hides the pose during a job. Read the measured flange pose.
            pose = self._control.read_pose()
            self._tray_scene.vacuum_completed(targets, {"actual": list(pose)})

    def describe(self):
        return {**super().describe(), "demo": True}

    def snapshot(self):
        return {**super().snapshot(), "demo": True}


class Rehearsal:
    def __init__(self, directory, required, *, identity, robot_state_provider=None):
        self.directory, self.required, self.identity = (
            Path(directory),
            tuple(required),
            validate_client(identity),
        )
        if set(self.required) - DEMO_SERVICES:
            raise ValueError("Rehearsal has no demo factory for a required service.")
        self.clients = {}
        self.simulator = self.robot_state = None
        self._robot_state_provider = robot_state_provider
        self.calibration_references_file = None
        self.tray_scene = None
        self._stack = ExitStack()
        self._condition = threading.Condition(threading.RLock())
        self._entered = self._starting = self._active = False
        self._operations = 0
        self._endpoints = {}
        self._identities = {self.identity}

    def __enter__(self):
        with self._condition:
            if self._entered:
                raise RuntimeError("A rehearsal context can start only once.")
            self._entered = self._starting = True
        try:
            self.directory.mkdir(mode=0o750, parents=True, exist_ok=True)
            tray_path = os.environ.get("MG400_ORCH_TRAY_FILE")
            if tray_path and {"robot", "vision"} & set(self.required):
                from vision.tray import validate_tray

                from .skills.vision import _read_tray

                tray = validate_tray(tomllib.loads(_read_tray(tray_path).decode("utf-8")))
                if tray != demo_tray_document():
                    raise ValueError("Tray rehearsal requires its explicit synthetic tray fixture.")
                self.tray_scene = DemoTrayScene(self._owned_robot_state)
                self._stack.callback(self.tray_scene.close)
            for name in self.required:
                getattr(self, f"_{name}")()
                self._stack.callback(self.clients[name].close)
            for client in self.clients.values():
                if client.describe().get("demo") is not True:
                    raise RuntimeError("Rehearsal refuses a service without demo=true.")
            with self._condition:
                self._active = True
            return self
        except BaseException:
            self._stack.close()
            raise
        finally:
            with self._condition:
                self._starting = False
                self._condition.notify_all()

    def new_clients(self, identity, *, profile="runner"):
        """Create unacquired, fresh identities for these same active demos.

        The console profile also reads events. Neither profile renews a lease
        on its own.

        The context owns their lifetime. Callers must stop and drain work before
        leaving the context; it never re-creates servers or acquires a lease.
        """
        validate_client(identity)
        created = {}
        try:
            with self._condition:
                if not self._active:
                    raise RuntimeError("Fresh clients require an active rehearsal context.")
                if identity in self._identities:
                    raise ValueError("A rehearsal client identity cannot be reused.")
                self._identities.add(identity)
                for name, (adapter, url) in self._endpoints.items():
                    client = _DemoClient(self, adapter(url, identity), profile=profile)
                    self._stack.callback(client.close)
                    created[name] = client
            return created
        except BaseException:
            for client in created.values():
                client.close()
            raise

    def _owned_robot_state(self):
        if self.robot_state is not None:
            return self.robot_state
        return None if self._robot_state_provider is None else self._robot_state_provider()

    def _robot(self):
        # SIM-01 uses the controller's three standard ports. Bind those ports
        # atomically on a fresh numeric loopback address; never reuse a live rig.
        for _ in range(32):
            host = f"127.203.{secrets.randbelow(254) + 1}.{secrets.randbelow(254) + 1}"
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError("Rehearsal addresses must be loopback.")
            feedback = self.directory / "gripper" / "feedback.json"
            simulator = Simulator(host, gripper_feedback=feedback)
            # Set only the owned simulation's initial state. Never send an
            # initialization command or use a host collector path.
            simulator.collector.set(init=1, state=1, position=1000)
            if self.tray_scene is not None:
                simulator.controller.pose = DEMO_TRAY_INITIAL_FLANGE
            try:
                simulator.start()
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE:
                    raise
            else:
                self.simulator = simulator
                self._stack.callback(simulator.close)
                break
        else:
            raise RuntimeError("No free loopback controller ports.")
        panel_path = Path(__file__).resolve().parents[2] / "apps/control_panel/server.py"
        if not panel_path.is_file():
            raise RuntimeError("Robot rehearsal requires the repository checkout's control panel.")
        panel_server = runpy.run_path(str(panel_path))["PanelServer"]
        state = self.robot_state = _DemoRobot(
            host, self.directory / "controller-locks", feedback, tray_scene=self.tray_scene
        )
        self._stack.callback(state.close)
        server = panel_server(("127.0.0.1", 0), state)
        self._serve(server)
        self._stack.callback(server.stop_sockets)
        url = f"http://127.0.0.1:{server.server_port}"
        self._endpoints["robot"] = (RobotAdapter, url)
        self.clients["robot"] = RobotAdapter(url, self.identity)

    def _serve(self, server):
        self._stack.callback(server.server_close)
        worker = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.02},
            name="orch-demo-http",
            daemon=True,
        )
        worker.start()

        def close():
            server.shutdown()
            worker.join(2)

        self._stack.callback(close)

    def _stage(self):
        from werkzeug.serving import make_server

        from stage_controller.app import create_app
        from stage_controller.controller import MotorController

        controller = MotorController(demo=True)
        self._stack.callback(controller.close)
        controller.connect("DEMO")
        controller.start()
        server = make_server("127.0.0.1", 0, create_app(controller=controller), threaded=True)
        self._serve(server)
        url = f"http://127.0.0.1:{server.server_port}/contract"
        self._endpoints["stage"] = (ServiceAdapter, url)
        self.clients["stage"] = ServiceAdapter(url, self.identity)

    def _thermal(self):
        # FastAPI stays on its own ASGI server, bound from an already reserved
        # loopback socket. No HTTP client or environment can select a camera.
        import socket

        import uvicorn

        from thermal_cam.app import create_app
        from thermal_cam.service import CameraService

        camera = CameraService(
            demo=True,
            roi_path=os.environ.get("MG400_ORCH_CRUCIBLE_ROIS_FILE")
            or self.directory / "absent-rois.toml",
        )
        app = create_app(service=camera)
        listener = socket.socket()
        self._stack.callback(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))
        worker = threading.Thread(
            target=server.run, kwargs={"sockets": [listener]}, name="orch-demo-thermal", daemon=True
        )
        worker.start()

        def close():
            server.should_exit = True
            worker.join(5)

        self._stack.callback(close)
        import time

        deadline = time.monotonic() + 5
        while not server.started:
            if not worker.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("Thermal rehearsal startup failed.")
            time.sleep(0.01)
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        self._endpoints["thermal"] = (ThermalAdapter, url)
        self.clients["thermal"] = ThermalAdapter(url, self.identity)

    def _furnace(self):
        from werkzeug.serving import make_server

        from furnace.app import create_app, make_service

        service = make_service(demo=True)
        self._stack.callback(service.close)
        service.start()
        server = make_server("127.0.0.1", 0, create_app(service), threaded=True)
        self._serve(server)
        url = f"http://127.0.0.1:{server.server_port}"
        self._endpoints["furnace"] = (FurnaceAdapter, url)
        self.clients["furnace"] = FurnaceAdapter(url, self.identity)

    def _vision(self):
        from werkzeug.serving import make_server

        from vision.service import VisionService, create_app

        frames = self.tray_scene or _DemoCalibrationFrames(self._owned_robot_state)
        self._stack.callback(frames.close)
        directory = self.directory / "vision-calibration"
        directory.mkdir(mode=0o750)
        references = directory / "references.toml"
        descriptor = os.open(
            references,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            for identity, pose in DEMO_CALIBRATION_REFERENCES:
                target.write(f"[[references]]\nid = {identity}\nflange_mm = {list(pose)}\n")
        self.calibration_references_file = references

        class EmptyModel:
            def predict(self, image):
                return image, {
                    "image_size": {"width": image.width, "height": image.height},
                    "coordinate_system": "oriented_original_image_pixels",
                    "counts": {},
                    "detections": [],
                }

        service = VisionService(
            demo=True,
            frames=frames,
            localizer_factory=(self.tray_scene.localizer if self.tray_scene else EmptyModel),
            calibration_directory=directory,
        )
        if self.tray_scene is not None:
            service.calibrations.save(DEMO_CALIBRATION_DEVICE, self.tray_scene.calibration_model())
        self._stack.callback(service.close)
        service.start()
        server = make_server("127.0.0.1", 0, create_app(service), threaded=True)
        self._serve(server)
        url = f"http://127.0.0.1:{server.server_port}"
        self._endpoints["vision"] = (ServiceAdapter, url)
        self.clients["vision"] = ServiceAdapter(url, self.identity)

    def __exit__(self, *exc):
        with self._condition:
            while self._starting:
                self._condition.wait()
            self._active = False
            while self._operations:
                self._condition.wait()
        self._stack.close()
