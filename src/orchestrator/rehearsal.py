"""Own isolated loopback demo services. Never load production endpoint settings."""

import errno
import ipaddress
import runpy
import secrets
import threading
from contextlib import ExitStack
from pathlib import Path

from mg400.controller_ownership import ControllerOwnership
from mg400.sim import Simulator
from mg400.web_control import RobotPanelState

from .clients import RobotAdapter, ServiceAdapter


class _DemoRobot(RobotPanelState):
    def __init__(self, host, lock_directory):
        super().__init__(host)
        self._ownership = ControllerOwnership(host, directory=lock_directory)

    def describe(self):
        return {**super().describe(), "demo": True}

    def snapshot(self):
        return {**super().snapshot(), "demo": True}


class Rehearsal:
    def __init__(self, directory, required, *, identity):
        self.directory, self.required, self.identity = Path(directory), tuple(required), identity
        if set(self.required) - {"robot", "stage", "thermal"}:
            raise ValueError("Rehearsal has no demo factory for a required service.")
        self.clients = {}
        self.simulator = self.robot_state = None
        self._stack = ExitStack()

    def __enter__(self):
        try:
            self.directory.mkdir(mode=0o750, parents=True, exist_ok=True)
            for name in self.required:
                getattr(self, f"_{name}")()
                self._stack.callback(self.clients[name].close)
            for client in self.clients.values():
                if client.describe().get("demo") is not True:
                    raise RuntimeError("Rehearsal refuses a service without demo=true.")
            return self
        except BaseException:
            self._stack.close()
            raise

    def _robot(self):
        # SIM-01 uses the controller's three standard ports. Bind those ports
        # atomically on a fresh numeric loopback address; never reuse a live rig.
        for _ in range(32):
            host = f"127.203.{secrets.randbelow(254) + 1}.{secrets.randbelow(254) + 1}"
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError("Rehearsal addresses must be loopback.")
            simulator = Simulator(host)
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
        state = self.robot_state = _DemoRobot(host, self.directory / "controller-locks")
        self._stack.callback(state.close)
        server = panel_server(("127.0.0.1", 0), state)
        self._serve(server)
        self._stack.callback(server.stop_sockets)
        self.clients["robot"] = RobotAdapter(
            f"http://127.0.0.1:{server.server_port}", "", self.identity
        )

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
        self.clients["stage"] = ServiceAdapter(
            f"http://127.0.0.1:{server.server_port}/contract", "", self.identity
        )

    def _thermal(self):
        # FastAPI stays on its own ASGI server, bound from an already reserved
        # loopback socket. No HTTP client or environment can select a camera.
        import socket

        import uvicorn

        from thermal_cam.app import create_app
        from thermal_cam.service import CameraService

        camera = CameraService(demo=True, roi_path=self.directory / "absent-rois.toml")
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
        self.clients["thermal"] = ServiceAdapter(
            f"http://127.0.0.1:{listener.getsockname()[1]}", "", self.identity
        )

    def __exit__(self, *exc):
        self._stack.close()
