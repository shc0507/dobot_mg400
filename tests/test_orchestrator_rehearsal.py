"""Run complete recipes against owned loopback services, never lab devices."""

import ipaddress
import socket
import threading
import time
import uuid
from pathlib import Path

import pytest

from orchestrator.clients import RobotAdapter, ServiceAdapter
from orchestrator.recipe import load_recipe, validate
from orchestrator.rehearsal import Rehearsal
from orchestrator.runner import Runner
from orchestrator.skills import default_registry
from orchestrator.store import RunStore

RECIPES = sorted((Path(__file__).resolve().parents[1] / "recipes").glob("*.toml"))


@pytest.fixture(autouse=True)
def numeric_loopback_only(monkeypatch):
    import serial
    import usb.core

    original = socket.socket.connect
    connected = []

    def connect(sock, address):
        assert ipaddress.ip_address(address[0]).is_loopback
        connected.append(address)
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)

    def no_device(*args, **kwargs):
        pytest.fail("Rehearsal attempted real device access")

    monkeypatch.setattr(serial, "Serial", no_device)
    monkeypatch.setattr(usb.core, "find", no_device)
    return connected


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("Owned rehearsal did not reach expected state")
        time.sleep(0.01)


@pytest.mark.parametrize("path", RECIPES, ids=lambda path: path.name)
def test_every_recipe_rehearses_without_hardware(tmp_path, path, monkeypatch):
    assert RECIPES
    if path.name == "tray-transfer.toml":
        from test_recipe_tray_transfer import run_tray_rehearsal

        def no_port(*args, **kwargs):
            pytest.fail("The new tray acceptance case must not bind or connect a port")

        with monkeypatch.context() as patch:
            for method in ("bind", "listen", "connect"):
                patch.setattr(socket.socket, method, no_port)
            result = run_tray_rehearsal(tmp_path, patch)
        assert result["status"] == "completed", result
        assert all(step["status"] == "completed" for step in result["row"]["steps"])
        assert result["occupancy"] == {"source": False, "target": True}
        return
    registry = default_registry()
    recipe = load_recipe(path, registry)
    identity = "orch-" + uuid.uuid4().hex
    with (
        RunStore(tmp_path / "runs") as store,
        Rehearsal(tmp_path / "demos", recipe.services, identity=identity) as demo,
    ):
        runner = Runner(recipe, registry, demo.clients, store, time_scale=0.01).start()
        try:
            assert runner.wait(15) == "completed", runner.snapshot()
            row = store.get(runner.run_id)
            assert all(step["status"] == "completed" for step in row["steps"])
            if path.name == "example-rehearsal.toml":
                assert row["samples"][0]["chip_id"] == recipe.params["chip_id"]
                assert demo.simulator.controller.pose == (230, 0, 20, 0)
        finally:
            runner.abort()
            runner._thread.join(5)
            assert not runner._thread.is_alive()


def test_failing_step_holds_owned_robot_and_resumes_after_explicit_manual_recovery(tmp_path):
    registry = default_registry()
    calls = []

    def fail_with_motion(ctx):
        # Arm keyboard control through the public robot service, then fail
        # before the next recipe step. No direct controller writes.
        robot = ctx.clients["robot"]
        robot.command("keyboard", enabled=True, speed=1, seq=1)
        calls.append("failed-step")
        raise RuntimeError("synthetic failure while robot control is armed")

    def forbidden(ctx):
        calls.append("later-step")

    registry.register("test.fail", fail_with_motion, services=("robot",))
    registry.register("test.later", forbidden, services=("robot",))
    recipe = validate(
        {
            "name": "Failure rehearsal",
            "version": 1,
            "params": {},
            "steps": [
                {"skill": name, "params": {}, "timeout_s": 5}
                for name in ("robot.enable", "test.fail", "test.later")
            ],
        },
        registry,
    )
    with (
        RunStore(tmp_path / "runs") as store,
        Rehearsal(tmp_path / "demos", recipe.services, identity="orch-failure-demo") as demo,
    ):
        runner = Runner(recipe, registry, demo.clients, store).start()
        try:
            assert runner.wait(10) == "held", runner.snapshot()
            wait_for(lambda: runner._settled)
            assert calls == ["failed-step"]
            assert runner.snapshot()["stops"]["robot"]["stop"] == "confirmed"
            state = demo.robot_state.snapshot()
            assert state["busy"] is False and state["keyboard_enabled"] is False
            assert state["controller_id"] == "orch-failure-demo"
            assert "ResetRobot()" in demo.simulator.controller.sent()
            assert store.get(runner.run_id)["steps"][2]["status"] == "pending"
            assert "fault" not in state and "connected" not in state
            assert state["locked"] is True
            with pytest.raises(RuntimeError, match="manual recovery"):
                runner.clients.reconcile()
            # An explicit operator-equivalent recovery on this owned simulator
            # confirms disable/rest. Reconciliation never clears the lock itself.
            demo.clients["robot"].run("disable", timeout=3)
            reconciled = runner.clients.reconcile()["robot"]
            assert reconciled["instance"] == state["instance"]
            assert reconciled["robot_error"] is False
            assert reconciled["stop_unconfirmed"] is False
            assert reconciled["locked"] is False and reconciled["busy"] is False
            assert calls == ["failed-step"]
            runner.resume(manually_completed=True, note="Disabled and completed the step manually.")
            wait_for(lambda: runner.status == "completed")
            assert calls == ["failed-step", "later-step"]
            assert store.get(runner.run_id)["steps"][1]["status"] == "manually_completed"
            stops = [
                event["data_json"]
                for event in store.get(runner.run_id)["events"]
                if event["kind"] == "stop"
            ]
            assert any("verified-disabled-rest" in event for event in stops)
        finally:
            runner.abort()
            runner._thread.join(5)
            assert not runner._thread.is_alive()


def test_fresh_clients_use_same_owned_endpoints_without_acquiring(
    tmp_path, monkeypatch, numeric_loopback_only
):
    endpoints = {}
    original = ServiceAdapter.__init__

    def construct(self, url, identity, **kwargs):
        endpoints.setdefault(identity, []).append(url)
        return original(self, url, identity, **kwargs)

    monkeypatch.setattr(ServiceAdapter, "__init__", construct)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(key, "http://production.invalid:9")
    for name in ("ROBOT", "STAGE", "THERMAL"):
        monkeypatch.setenv(f"MG400_ORCH_{name}_URL", "http://production.invalid:9")
        monkeypatch.setenv(f"MG400_ORCH_{name}_TOKEN", "production-secret-must-not-be-used")
    with Rehearsal(tmp_path, ("robot", "stage", "thermal"), identity="initial") as demo:
        simulator = demo.simulator
        before = len(numeric_loopback_only)
        fresh = demo.new_clients("fresh-transaction")
        assert len(numeric_loopback_only) == before
        assert set(fresh) == set(demo.clients) == {"robot", "stage", "thermal"}
        assert endpoints["initial"] == endpoints["fresh-transaction"]
        assert demo.simulator is simulator
        for name, client in fresh.items():
            assert client is not demo.clients[name]
            assert (
                client.identity == "fresh-transaction" and demo.clients[name].identity == "initial"
            )
            assert client.describe()["demo"] is True
            assert not client.owned(client.state())
            client.acquire()
            assert client.owned(client.state())
            assert client.stop()["stop"] == "confirmed"
            client.release()
            client.close()
        with pytest.raises(ValueError, match="cannot be reused"):
            demo.new_clients("fresh-transaction")
    for client in fresh.values():
        for method in (client.acquire, client.state, client.stop, client.release):
            with pytest.raises(RuntimeError, match="no longer active"):
                method()
        client.close()


@pytest.mark.parametrize("identity", ["", "\n", "x" * 129, None, 1])
def test_fresh_client_identity_is_validated_before_any_startup(tmp_path, identity):
    demo = Rehearsal(tmp_path, (), identity="initial")
    with pytest.raises(ValueError):
        demo.new_clients(identity)


def test_fresh_factory_requires_active_context_and_rejects_initial_identity(tmp_path):
    demo = Rehearsal(tmp_path, (), identity="initial")
    with pytest.raises(RuntimeError, match="active rehearsal"):
        demo.new_clients("before")
    with demo:
        with pytest.raises(ValueError, match="cannot be reused"):
            demo.new_clients("initial")
        assert demo.new_clients("valid") == {}
    with pytest.raises(RuntimeError, match="active rehearsal"):
        demo.new_clients("after")
    with pytest.raises(RuntimeError, match="only once"):
        demo.__enter__()


def test_fresh_factory_is_unavailable_after_startup_failure(tmp_path, monkeypatch):
    def failure(self):
        raise RuntimeError("Synthetic startup failure")

    monkeypatch.setattr(Rehearsal, "_stage", failure)
    demo = Rehearsal(tmp_path, ("stage",), identity="initial")
    with pytest.raises(RuntimeError, match="startup failure"):
        demo.__enter__()
    with pytest.raises(RuntimeError, match="active rehearsal"):
        demo.new_clients("after-failure")


def test_partial_fresh_factory_failure_closes_only_its_new_adapters(tmp_path, monkeypatch):
    closed = []
    original_init, original_close = ServiceAdapter.__init__, RobotAdapter.close

    def construct(self, url, identity, **kwargs):
        if identity == "broken" and url.endswith("/contract"):
            raise ValueError("Synthetic adapter failure")
        return original_init(self, url, identity, **kwargs)

    def close(self):
        closed.append(self.identity)
        return original_close(self)

    monkeypatch.setattr(ServiceAdapter, "__init__", construct)
    monkeypatch.setattr(RobotAdapter, "close", close)
    with Rehearsal(tmp_path, ("robot", "stage"), identity="initial") as demo:
        with pytest.raises(ValueError, match="adapter failure"):
            demo.new_clients("broken")
        assert closed == ["broken"]
        assert demo.clients["robot"].describe()["demo"]
        assert set(demo.new_clients("next")) == {"robot", "stage"}


def test_context_closes_fresh_robot_workers_without_reusing_old_identity(tmp_path):
    with Rehearsal(tmp_path, ("robot",), identity="initial") as demo:
        old = demo.clients["robot"]
        old.acquire()
        instance = old.state()["instance"]
        old.stop()
        old.release()
        old.close()
        fresh = demo.new_clients("recovery")["robot"]
        previous_workers = set(threading.enumerate())
        fresh.acquire()
        assert fresh.state()["instance"] == instance
        assert fresh.owned(fresh.state())
        # Closing the original identity again cannot release the fresh owner.
        old.close()
        assert fresh.owned(fresh.state())
        workers = [
            worker
            for worker in threading.enumerate()
            if worker not in previous_workers and worker.name.startswith("RobotClient-")
        ]
        assert len(workers) == 2 and all(worker.is_alive() for worker in workers)
    assert all(not worker.is_alive() for worker in workers)
    with pytest.raises(RuntimeError, match="no longer active"):
        fresh.acquire()


def test_fresh_client_io_does_not_hold_factory_lock_and_exit_drains_registered_calls(
    tmp_path, monkeypatch
):
    entered, finish, stopped = threading.Event(), threading.Event(), threading.Event()
    original = ServiceAdapter.state

    def state(self):
        if self.identity == "blocked":
            entered.set()
            finish.wait()
        return original(self)

    monkeypatch.setattr(ServiceAdapter, "state", state)
    demo = Rehearsal(tmp_path, ("stage",), identity="initial")
    demo.__enter__()
    first = demo.new_clients("blocked")["stage"]
    errors = []

    def read():
        try:
            first.state()
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=read)
    worker.start()
    closer = None
    try:
        assert entered.wait(1)
        second = demo.new_clients("independent")["stage"]
        assert second.stop()["stop"] == "confirmed"

        def close():
            demo.__exit__(None, None, None)
            stopped.set()

        closer = threading.Thread(target=close)
        closer.start()
        wait_for(lambda: not demo._active)
        assert not stopped.is_set()
        with pytest.raises(RuntimeError, match="no longer active"):
            second.acquire()
        finish.set()
        worker.join(2)
        closer.join(2)
        assert stopped.is_set() and errors == []
    finally:
        finish.set()
        worker.join(2)
        if closer is not None:
            closer.join(2)
        else:
            demo.__exit__(None, None, None)


@pytest.fixture
def tray_scene(tmp_path, monkeypatch):
    """Owned image/math fixtures: no sockets, device libraries, or native capture."""
    import copy
    import ctypes

    import cv2

    from orchestrator.rehearsal import DEMO_TRAY_INITIAL_FLANGE, DemoTrayScene

    def forbidden(*args, **kwargs):
        pytest.fail("New tray rehearsal coverage must remain in-process")

    monkeypatch.setattr(socket.socket, "bind", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(cv2, "VideoCapture", forbidden)
    monkeypatch.setattr(ctypes, "CDLL", forbidden)

    class Robot:
        def __init__(self):
            self.value = {
                "demo": True,
                "feedback_fresh": True,
                "busy": False,
                "actual": [*DEMO_TRAY_INITIAL_FLANGE, 1000],
                "gripper": {"outcome": None},
            }

        def snapshot(self):
            return copy.deepcopy(self.value)

    robot = Robot()
    scenes = []

    def make(failure=None):
        scene = DemoTrayScene(lambda: robot, failure=failure)
        scenes.append(scene)
        return scene, robot

    yield make
    for scene in scenes:
        scene.close()


def test_synthetic_tray_configuration_is_explicit_private_and_request_bounded(tray_scene, tmp_path):
    import json
    import os

    from orchestrator.rehearsal import demo_tray_document, write_demo_tray_configuration
    from orchestrator.stations import StationStore, Workspace
    from vision.service import MAX_REQUEST_BYTES
    from vision.tray import load_tray

    before = dict(os.environ)
    settings = write_demo_tray_configuration(tmp_path / "owned-fixture")
    assert dict(os.environ) == before
    assert not any("TOKEN" in name or name.endswith("_URL") for name in settings)
    assert load_tray(settings["MG400_ORCH_TRAY_FILE"]) == demo_tray_document()
    stations = StationStore(
        Workspace.from_settings(settings), settings["MG400_ORCH_STATIONS_FILE"]
    ).load()
    assert set(stations) == {"source", "target", "drift-check"}
    assert stations["drift-check"]["z_mm"] == 34
    assert all(station["zone"] == "tray" for station in stations.values())
    for key in ("TRAY_FILE", "TOOL_FILE", "STATIONS_FILE", "ZONES_FILE"):
        assert Path(settings["MG400_ORCH_" + key]).stat().st_mode & 0o777 in {0o600, 0o640}
    document = {
        "action": "observe_slots",
        "camera": 1,
        "tray": demo_tray_document(),
        "targets": {
            name: [stations[name]["x_mm"], stations[name]["y_mm"]] for name in ("source", "target")
        },
        "axis": "long",
    }
    assert len(json.dumps(document, allow_nan=False).encode()) < MAX_REQUEST_BYTES
    with pytest.raises(FileExistsError):
        write_demo_tray_configuration(tmp_path / "owned-fixture")


def test_synthetic_tray_pixels_match_model_markers_and_chip_geometry(tray_scene):
    import hashlib

    from vision.calibration import detect_markers, drift_proof, robot_xy
    from vision.tray import locate_tray

    scene, robot = tray_scene()
    image = scene.read(1)
    model = scene.calibration_model()
    markers = detect_markers(image)
    assert {marker["id"] for marker in markers} == {0, 1, 2, 4, 7}
    proof = drift_proof(
        model, robot.value["actual"][:4], markers, image.size, device=model["camera_device"]
    )
    assert proof["passed"] is True and proof["distance_px"] < 1
    location = locate_tray(image, model, scene.tray, device=model["camera_device"])
    assert set(location["slots"]) == {"source", "target"}
    assert location["slots"]["source"]["reference_xy_mm"] == pytest.approx([220, 20], abs=0.5)
    _, result = scene.localizer().predict(image)
    assert result["counts"] == {"Chip": 1}
    center = robot_xy(model, result["detections"][0]["center_xy"], image.size)
    assert center == pytest.approx([220, 20], abs=0.5)
    assert (
        hashlib.sha256(image.tobytes()).digest() != hashlib.sha256(scene.read(1).tobytes()).digest()
    )


@pytest.mark.parametrize(
    "failure,initial,final",
    [
        (None, {"source": True, "target": False}, {"source": False, "target": True}),
        ("no-source", {"source": False, "target": False}, {"source": False, "target": False}),
        ("occupied-target", {"source": True, "target": True}, {"source": False, "target": True}),
        ("no-target", {"source": True, "target": False}, {"source": False, "target": False}),
        ("source-remains", {"source": True, "target": False}, {"source": True, "target": True}),
    ],
)
def test_synthetic_tray_occupancy_changes_only_on_completed_grips(
    tray_scene, failure, initial, final
):
    scene, robot = tray_scene(failure)
    assert scene.occupancy == initial
    old_image = scene.read(1)
    robot.value.update(actual=[220, 20, 100, 0, 600], gripper={"outcome": None})
    scene.gripper_completed(400, robot.snapshot())
    assert scene.occupancy == initial
    answer = scene.collector_answer({"position": 400})
    assert answer["state"] == (2 if initial["source"] else 1)
    robot.value["gripper"]["outcome"] = "contact" if initial["source"] else "arrived"
    scene.gripper_completed(400, robot.snapshot())
    robot.value.update(actual=[260, 20, 100, 0, 900], gripper={"outcome": "arrived"})
    scene.gripper_completed(900, robot.snapshot())
    assert scene.occupancy == final
    # Inference reads only the supplied image, not the scene's changed occupancy.
    assert len(scene.localizer().predict(old_image)[1]["detections"]) == sum(initial.values())


def test_synthetic_moved_camera_fails_drift_without_changing_installed_model(tray_scene):
    from vision.calibration import detect_markers, drift_proof

    scene, robot = tray_scene("moved-camera")
    model = scene.calibration_model()
    image = scene.read(1)
    proof = drift_proof(
        model,
        robot.value["actual"][:4],
        detect_markers(image),
        image.size,
        device=model["camera_device"],
    )
    assert proof["passed"] is False and proof["distance_px"] > 3
    assert scene.calibration_model() == model


def test_synthetic_separate_vision_pool_reads_only_its_owned_robot_scene(tray_scene):
    first, robot = tray_scene()
    second, _ = tray_scene()
    robot._tray_scene = first
    robot.value.update(actual=[220, 20, 100, 0, 600], gripper={"outcome": "contact"})
    first.gripper_completed(400, robot.snapshot())
    robot.value.update(actual=[260, 20, 100, 0, 900], gripper={"outcome": "arrived"})
    first.gripper_completed(900, robot.snapshot())
    robot.value["actual"] = [210, 10, 180, 0, 900]
    from vision.calibration import robot_xy

    image = second.read(1)
    detection = second.localizer().predict(image)[1]["detections"][0]
    assert robot_xy(
        second.calibration_model(), detection["center_xy"], image.size
    ) == pytest.approx([260, 20], abs=0.5)


def test_tray_rehearsal_rejects_other_geometry_before_starting_services(
    tray_scene, tmp_path, monkeypatch
):
    from orchestrator.rehearsal import write_demo_tray_configuration

    settings = write_demo_tray_configuration(tmp_path / "synthetic")
    path = Path(settings["MG400_ORCH_TRAY_FILE"])
    path.write_text(
        path.read_text().replace("Owned synthetic rehearsal tray", "Other measured tray")
    )
    monkeypatch.setenv("MG400_ORCH_TRAY_FILE", str(path))
    with pytest.raises(ValueError, match="explicit synthetic tray"):
        with Rehearsal(tmp_path / "demos", ("robot", "vision"), identity="in-process-only"):
            pytest.fail("Unmatched geometry must not start a rehearsal")


@pytest.mark.parametrize("kind", ["fifo", "symlink"])
def test_tray_rehearsal_rejects_unsafe_input_before_read_or_factory(
    tray_scene, tmp_path, monkeypatch, kind
):
    import io
    import os

    from orchestrator.rehearsal import write_demo_tray_configuration

    settings = write_demo_tray_configuration(tmp_path / "synthetic")
    path = Path(settings["MG400_ORCH_TRAY_FILE"])
    original = path.with_name("original.toml")
    path.rename(original)
    if kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.symlink_to(original.name)
    monkeypatch.setenv("MG400_ORCH_TRAY_FILE", str(path))
    factory_calls, readable_opens = [], []
    open_file, open_fd = io.open, os.open

    def checked_file(file, *args, **kwargs):
        if not isinstance(file, int) and Path(file) == path:
            readable_opens.append(str(file))
            pytest.fail("Unsafe tray input reached a readable open")
        return open_file(file, *args, **kwargs)

    def checked_fd(file, flags, *args, **kwargs):
        if str(file) in {str(path), path.name} and not flags & os.O_PATH:
            readable_opens.append(str(file))
            pytest.fail("Unsafe tray input reached a readable descriptor")
        return open_fd(file, flags, *args, **kwargs)

    def factory(self):
        factory_calls.append("service")
        pytest.fail("Unsafe tray input reached a service factory")

    monkeypatch.setattr(io, "open", checked_file)
    monkeypatch.setattr(os, "open", checked_fd)
    monkeypatch.setattr(Rehearsal, "_robot", factory)
    monkeypatch.setattr(Rehearsal, "_vision", factory)
    with pytest.raises(ValueError, match="safe file types"):
        with Rehearsal(tmp_path / "demos", ("robot", "vision"), identity="in-process-only"):
            pytest.fail("Unsafe tray input must refuse startup")
    assert factory_calls == readable_opens == []
