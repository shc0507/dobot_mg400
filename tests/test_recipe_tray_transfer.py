"""Vision-guided transfer with synthetic pixels and in-process clients only."""

import copy
import socket
import threading
from pathlib import Path

import numpy as np
import pytest
from test_orchestrator_transfer import Robot

from orchestrator.recipe import load_recipe
from orchestrator.rehearsal import (
    DEMO_CALIBRATION_DEVICE,
    DEMO_TRAY_INITIAL_FLANGE,
    DemoTrayScene,
    write_demo_tray_configuration,
)
from orchestrator.runner import Runner
from orchestrator.skills import default_registry
from orchestrator.skills.vision import _TrayInputs
from orchestrator.store import RunStore
from vision.service import VisionService, _slot_arguments, _slot_observations
from vision.tray import locate_tray

RECIPE = Path(__file__).resolve().parents[1] / "recipes/tray-transfer.toml"


@pytest.fixture(autouse=True)
def no_ports_or_devices(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Tray acceptance must not use sockets or devices")

    for name in ("bind", "listen", "connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    import serial
    import usb.core

    monkeypatch.setattr(serial, "Serial", forbidden)
    monkeypatch.setattr(usb.core, "find", forbidden)


class TrayRobot(Robot):
    identity = "owned-run"
    timeout = 0.1

    def __init__(self, failure=None):
        super().__init__()
        self.value.update(demo=True, actual=[*DEMO_TRAY_INITIAL_FLANGE, 1000])
        self.failure = failure
        self.empty = failure == "empty-grasp"
        self.scene = None
        self.acquired = False
        self.stops = 0
        self.lock = threading.RLock()

    def snapshot(self):
        return self.state()

    def state(self):
        with self.lock:
            return super().state()

    def describe(self):
        return {"contract_version": 1, **super().describe()}

    def acquire(self):
        self.acquired = True

    def heartbeat(self):
        assert self.acquired

    def healthy(self, state):
        return state["syncing"] and state["feedback_fresh"]

    def run(self, action, *, timeout=5, **params):
        with self.lock:
            assert self.acquired
            previous = self.value["gripper"]["outcome"]
            super().run(action, **params)
            if action == "grip":
                self.scene.gripper_completed(params["position"], self.state())
            if action == "execute" and previous == "contact" and self.failure == "drop":
                self.value["gripper"] = {"outcome": "dropped", "outcome_at": len(self.calls)}
            return {"ok": True}

    def stop(self):
        with self.lock:
            self.stops += 1
            self.value["locked"] = True
            return {"stop": "confirmed"}

    def release(self):
        self.acquired = False

    def close(self):
        pass


class InProcessVision:
    identity = "owned-run"
    timeout = 0.1

    def __init__(self, service):
        self.service = service
        self.calls = []

    def describe(self):
        return self.service.describe()

    def acquire(self):
        self.service.lease.acquire(self.identity)

    def heartbeat(self):
        self.service.lease.renew(self.identity)

    def owned(self, state):
        return state["lease"]["holder"] == self.identity

    def healthy(self, state):
        return state["connected"] is True

    def state(self):
        return self.service.snapshot()

    def command(self, action, **params):
        self.calls.append((action, copy.deepcopy(params)))
        with self.service.lease.control(self.identity):
            identity = self.service.command(action, params, self.identity)
        return {"command_id": identity}

    def stop(self):
        return {"stop": self.service.stop(self.identity, wait_s=1)}

    def release(self):
        self.service.lease.release(self.identity)

    def close(self):
        pass


def run_tray_rehearsal(tmp_path, monkeypatch, *, failure=None, configure=None, change=None):
    """Shared all-recipes acceptance: real Runner/service, owned fakes, zero ports."""
    tmp_path.chmod(0o700)
    settings = write_demo_tray_configuration(tmp_path / "configuration")
    settings["MG400_ORCH_ZONE_STATE_FILE"] = str(tmp_path / "occupancy.json")
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    if configure:
        configure(settings)
    robot = TrayRobot(failure)
    scene = DemoTrayScene(
        lambda: robot, failure=None if failure in {"empty-grasp", "drop"} else failure
    )
    robot.scene = scene
    (tmp_path / "calibration").mkdir(mode=0o700)
    service = VisionService(
        demo=True,
        frames=scene,
        localizer_factory=scene.localizer,
        calibration_directory=tmp_path / "calibration",
    )
    service.calibrations.save(DEMO_CALIBRATION_DEVICE, scene.calibration_model())
    service.start()
    vision = InProcessVision(service)
    if change:
        change(robot, vision)
    registry = default_registry()
    recipe = load_recipe(RECIPE, registry, {"from_slot": "source", "to_slot": "target"})
    try:
        with RunStore(tmp_path / "runs") as store:
            runner = Runner(recipe, registry, {"robot": robot, "vision": vision}, store).start()
            try:
                status = runner.wait(12)
                report = runner.snapshot()
                row = store.get(runner.run_id)
                return {
                    "status": status,
                    "row": row,
                    "calls": copy.deepcopy(robot.calls),
                    "occupancy": scene.occupancy,
                    "snapshot": report,
                    "vision_calls": vision.calls,
                }
            finally:
                runner.abort()
                assert runner.wait_closed(5), runner.snapshot()
    finally:
        service.close()


def test_complete_recipe_uses_current_pixels_and_taught_motion(tmp_path, monkeypatch):
    result = run_tray_rehearsal(tmp_path, monkeypatch)
    assert result["status"] == "completed", result["snapshot"]
    assert result["occupancy"] == {"source": False, "target": True}
    assert [name for name, _ in result["vision_calls"]] == [
        "check_calibration",
        "observe_slots",
        "observe_slots",
    ]
    assert [params["position"] for name, params in result["calls"] if name == "grip"] == [
        900,
        400,
        900,
    ]
    assert {name for name, _ in result["calls"]} == {"execute", "grip"}
    assert all(params["speed"] == 1 for name, params in result["calls"] if name == "execute")


@pytest.mark.parametrize(
    "failure",
    [
        "no-source",
        "occupied-target",
        "no-target",
        "source-remains",
        "empty-grasp",
        "drop",
        "moved-camera",
    ],
)
def test_each_required_failure_holds_without_retry(tmp_path, monkeypatch, failure):
    result = run_tray_rehearsal(tmp_path, monkeypatch, failure=failure)
    assert result["status"] == "held", result["snapshot"]
    assert result["row"]["steps"][0]["status"] == "failed"
    assert result["row"]["steps"][0]["error"]
    if failure in {"no-source", "occupied-target", "moved-camera"}:
        assert not result["calls"]
    assert sum(name == "grip" and args["position"] == 400 for name, args in result["calls"]) <= 1


@pytest.mark.parametrize(
    "key",
    [
        "TRAY_FILE",
        "TRAY_CAMERA",
        "TRAY_GRIP",
        "TRAY_RELEASE",
        "CHIP_AXIS",
        "CHIP_R_OFFSET_DEG",
        "CHIP_R_MIN_DEG",
        "CHIP_R_MAX_DEG",
        "PRE_GRIP",
    ],
)
def test_missing_physical_input_holds_before_motion(tmp_path, monkeypatch, key):
    result = run_tray_rehearsal(
        tmp_path, monkeypatch, configure=lambda _: monkeypatch.delenv("MG400_ORCH_" + key)
    )
    assert result["status"] == "held"
    assert not result["calls"] and not result["vision_calls"]


@pytest.fixture
def scene(tmp_path):
    robot = TrayRobot()
    result = DemoTrayScene(lambda: robot)
    robot.scene = result
    yield robot, result
    result.close()


def evidence(scene):
    _, scene = scene
    image = scene.read(1)
    model = scene.calibration_model()
    location = locate_tray(np.asarray(image), model, scene.tray, device=DEMO_CALIBRATION_DEVICE)
    _, detections = scene.localizer().predict(image)
    return image, model, location, detections


@pytest.mark.parametrize(
    "mutation",
    [
        lambda result: result.pop("detections"),
        lambda result: result.update(counts={}),
        lambda result: result["detections"][0].update(polygon=None),
        lambda result: result["detections"][0].update(polygon=[[0, 0], [1, 1], [2, 2]]),
        lambda result: result["detections"][0].update(confidence=float("nan")),
        lambda result: result["detections"].append(copy.deepcopy(result["detections"][0])),
    ],
)
def test_missing_or_ambiguous_detection_cannot_mean_empty(scene, mutation):
    image, model, location, detections = evidence(scene)
    mutation(detections)
    with pytest.raises(ValueError):
        _slot_observations(image, model, location, detections, "long")


def test_robot_plane_axis_does_not_trust_raw_detector_angle(scene):
    image, model, location, detections = evidence(scene)
    detections["detections"][0]["angle_deg"] = 37
    long = _slot_observations(image, model, location, detections, "long")
    short = _slot_observations(image, model, location, detections, "short")
    angle = long["source"]["angle_deg"]
    assert min(angle, 180 - angle) < 1
    assert (short["source"]["angle_deg"] - angle) % 180 == pytest.approx(90)
    assert long["target"]["state"] == "empty"


def test_whole_request_bound_includes_tray_and_targets(scene):
    _, scene = scene
    args = {
        "camera": 1,
        "tray": scene.tray,
        "targets": {"source": [220, 20], "target": [260, 20]},
        "axis": "long",
    }
    assert _slot_arguments(args) == scene.tray
    args["tray"]["name"] = "x" * 17000
    with pytest.raises(ValueError):
        _slot_arguments(args)


@pytest.mark.parametrize(
    "angle,offset,low,high,expected",
    [
        (179, 0, -20, 20, -1),
        (1, 90, 80, 100, 91),
        (90, -90, -10, 10, 0),
    ],
)
def test_explicit_axis_convention_selects_unique_taught_r(angle, offset, low, high, expected):
    inputs = object.__new__(_TrayInputs)
    inputs.offset, inputs.minimum, inputs.maximum = offset, low, high
    assert inputs.flange_r(angle) == expected


def test_no_equivalent_angle_is_not_a_default():
    inputs = object.__new__(_TrayInputs)
    inputs.offset, inputs.minimum, inputs.maximum = 0, -20, 20
    with pytest.raises(RuntimeError, match="no unique"):
        inputs.flange_r(90)
