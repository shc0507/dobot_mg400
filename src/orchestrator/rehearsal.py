"""Own isolated loopback demo services. Never load production endpoint settings."""

import errno
import ipaddress
import math
import os
import runpy
import secrets
import threading
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

    def __init__(self, owner, adapter):
        self._owner, self._adapter = owner, adapter
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
        if name not in self._METHODS:
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
    def __init__(self, host, lock_directory, gripper_feedback):
        gripper = WebGripper(gripper_feedback)
        gripper.poll()
        super().__init__(host, gripper=gripper)
        self._ownership = ControllerOwnership(host, directory=lock_directory)

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

    def new_clients(self, identity):
        """Create unacquired, fresh identities for these same active demos.

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
                    client = _DemoClient(self, adapter(url, "", identity))
                    self._stack.callback(client.close)
                    created[name] = client
            return created
        except BaseException:
            for client in created.values():
                client.close()
            raise

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
        state = self.robot_state = _DemoRobot(host, self.directory / "controller-locks", feedback)
        self._stack.callback(state.close)
        server = panel_server(("127.0.0.1", 0), state)
        self._serve(server)
        self._stack.callback(server.stop_sockets)
        url = f"http://127.0.0.1:{server.server_port}"
        self._endpoints["robot"] = (RobotAdapter, url)
        self.clients["robot"] = RobotAdapter(url, "", self.identity)

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
        self.clients["stage"] = ServiceAdapter(url, "", self.identity)

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
        self.clients["thermal"] = ThermalAdapter(url, "", self.identity)

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
        self.clients["furnace"] = FurnaceAdapter(url, "", self.identity)

    def _vision(self):
        from werkzeug.serving import make_server

        from vision.service import VisionService, create_app

        def robot_state():
            if self.robot_state is not None:
                return self.robot_state
            return None if self._robot_state_provider is None else self._robot_state_provider()

        frames = _DemoCalibrationFrames(robot_state)
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
            localizer_factory=EmptyModel,
            calibration_directory=directory,
        )
        self._stack.callback(service.close)
        service.start()
        server = make_server("127.0.0.1", 0, create_app(service, token=""), threaded=True)
        self._serve(server)
        url = f"http://127.0.0.1:{server.server_port}"
        self._endpoints["vision"] = (ServiceAdapter, url)
        self.clients["vision"] = ServiceAdapter(url, "", self.identity)

    def __exit__(self, *exc):
        with self._condition:
            while self._starting:
                self._condition.wait()
            self._active = False
            while self._operations:
                self._condition.wait()
        self._stack.close()
