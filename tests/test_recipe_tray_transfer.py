"""Vision-guided transfer with synthetic pixels and in-process clients only."""

import copy
import json
import os
import socket
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from test_orchestrator_transfer import Context, Robot
from test_orchestrator_transfer import geometry as geometry

from orchestrator.recipe import load_recipe
from orchestrator.rehearsal import (
    DEMO_CALIBRATION_DEVICE,
    DEMO_TRAY_INITIAL_FLANGE,
    DemoTrayScene,
    write_demo_tray_configuration,
)
from orchestrator.runner import Runner
from orchestrator.skills import default_registry
from orchestrator.skills.robot import _RouteZones, _Transfer
from orchestrator.skills.vision import _read_tray, _TrayInputs
from orchestrator.store import RunStore
from orchestrator.tool import Pose, fingertip_to_flange
from vision.service import VisionService, _slot_arguments, _slot_observations, create_app
from vision.tray import locate_tray, validate_tray

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
    slot = args["tray"]["slots"][0]
    args["tray"]["slots"] = [
        {**copy.deepcopy(slot), "id": f"slot-{index:03d}-" + "x" * 50} for index in range(120)
    ]
    args["targets"] = {args["tray"]["slots"][0]["id"]: [220, 20]}
    assert validate_tray(args["tray"]) == args["tray"]
    assert len(json.dumps({"action": "observe_slots", **args}).encode()) > 16 * 1024
    with pytest.raises(ValueError, match="complete 16 KiB"):
        _slot_arguments(args)


def test_chip_overlapping_unrequested_groove_is_not_unique(scene):
    image, model, location, detections = evidence(scene)
    location["slots"]["unrequested"] = copy.deepcopy(location["slots"]["source"])
    with pytest.raises(ValueError, match="multiple configured"):
        _slot_observations(image, model, location, detections, "long", {"source", "target"})


def test_unrequested_invisible_groove_does_not_hide_requested_evidence(scene):
    image, model, location, detections = evidence(scene)
    location["slots"]["unrequested"] = {
        "centerline_xy_mm": [[10000, 10000], [10001, 10000]],
        "reference_xy_mm": [10000.5, 10000],
        "width_mm": 3,
    }
    result = _slot_observations(image, model, location, detections, "long", {"source", "target"})
    assert set(result) == {"source", "target"}
    assert result["source"]["state"] == "present"


def service_for(tmp_path, scene, **kwargs):
    directory = tmp_path / "service-calibration"
    directory.mkdir(mode=0o700)
    service = VisionService(
        demo=True,
        frames=scene,
        localizer_factory=scene.localizer,
        calibration_directory=directory,
        **kwargs,
    )
    service.calibrations.save(DEMO_CALIBRATION_DEVICE, scene.calibration_model())
    service.start()
    return service


@pytest.mark.parametrize("cancel", [None, "stop", "release", "expiry"])
def test_slot_command_uses_one_frame_model_and_fenced_publication(
    tmp_path, scene, monkeypatch, cancel
):
    _, scene = scene
    clock = [10.0]
    service = service_for(tmp_path, scene, clock=lambda: clock[0])
    app = create_app(service)
    client = app.test_client()
    headers = {
        "X-MG400-Client": "owned-run",
    }
    assert client.post("/api/heartbeat", json={}, headers=headers).status_code == 200
    counts = {"read": 0, "load": 0, "predict": 0}
    ready, proceed = threading.Event(), threading.Event()
    for owner, name in ((scene, "read"), (service.calibrations, "load")):
        original = getattr(owner, name)

        def counted(*args, _original=original, _name=name, **kwargs):
            counts[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(owner, name, counted)
    predict = service._model.predict

    def blocked(image):
        counts["predict"] += 1
        ready.set()
        assert proceed.wait(3)
        return predict(image)

    monkeypatch.setattr(service._model, "predict", blocked)
    try:
        response = client.post(
            "/api/command",
            headers=headers,
            json={
                "action": "observe_slots",
                "camera": 1,
                "tray": scene.tray,
                "targets": {"source": [220, 20], "target": [260, 20]},
                "axis": "long",
            },
        )
        assert response.status_code in {200, 202}, response.json
        assert ready.wait(3)
        if cancel == "stop":
            stranger = {**headers, "X-MG400-Client": "other-client"}
            assert client.post("/api/stop", headers=stranger, json={}).status_code == 200
        elif cancel == "release":
            assert client.post("/api/release", headers=headers, json={}).status_code == 200
            assert client.post("/api/heartbeat", headers=headers, json={}).status_code == 200
        elif cancel == "expiry":
            clock[0] += 4
            service.lease.poll()
        proceed.set()
        deadline = time.monotonic() + 3
        while service.snapshot()["busy"]:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        receipt = service.snapshot()["last_command"]
        assert receipt["result"] == ("ok" if cancel is None else "stopped")
        if cancel:
            assert receipt["output"] is None
        else:
            assert receipt["output"]["slots"]["source"]["state"] == "present"
        assert counts == {"read": 1, "load": 1, "predict": 1}
    finally:
        proceed.set()
        service.close()


@pytest.mark.parametrize(
    "headers,status",
    [({}, 400), ({"X-MG400-Client": "owned-run"}, 409)],
)
def test_slot_command_retains_control_guards(tmp_path, scene, headers, status):
    _, scene = scene
    service = service_for(tmp_path, scene)
    try:
        client = create_app(service).test_client()
        response = client.post(
            "/api/command",
            headers=headers,
            json={
                "action": "observe_slots",
                "camera": 1,
                "tray": scene.tray,
                "targets": {"source": [220, 20]},
                "axis": "long",
            },
        )
        assert response.status_code == status
        assert service.snapshot()["last_command"] is None
    finally:
        service.close()


def test_measured_copy_preserves_taught_geometry_and_post_pick_identity(geometry):
    ctx = Context()
    operation = _Transfer(ctx)
    original = copy.deepcopy(operation.stations)
    disk = geometry.path.read_bytes()
    observed = operation.measured_source("source", x_mm=231, y_mm=21, r_deg=91)
    source, target = operation.plan(observed, operation.route("target"))
    assert observed[-1]["z_mm"] == original["source"]["z_mm"]
    assert observed[-1]["approach_mm"] == original["source"]["approach_mm"]
    assert observed[-1]["via"] == original["source"]["via"]
    assert operation.stations == original and geometry.path.read_bytes() == disk
    with _RouteZones(ctx.zones) as zones:
        operation.pick(source, 400, 900, zones)
        expected = fingertip_to_flange(Pose(231, 21, 50, 91), operation.offset)
        assert ctx.robot.value["actual"][:4] == pytest.approx(
            [expected.x_mm, expected.y_mm, expected.z_mm, expected.r_deg]
        )
        before = len(ctx.robot.calls)
        taught = fingertip_to_flange(Pose(230, 20, 50, 90), operation.offset)
        ctx.robot.value["actual"][:4] = [taught.x_mm, taught.y_mm, taught.z_mm, taught.r_deg]
        with pytest.raises(RuntimeError):
            operation.place(target, 900, zones)
        assert len(ctx.robot.calls) == before


def test_measured_copy_cannot_alias_another_station(geometry):
    geometry.save("collision", Pose(241, 21, 30, 91), approach_mm=20)
    ctx = Context()
    operation = _Transfer(ctx)
    with pytest.raises(ValueError):
        operation.measured_source("source", x_mm=241, y_mm=21, r_deg=91)
    assert not ctx.robot.calls


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), 10000000])
def test_measured_copy_rejects_nonfinite_or_outside_workspace_before_motion(geometry, value):
    ctx = Context()
    with pytest.raises(ValueError):
        _Transfer(ctx).measured_source("source", x_mm=value, y_mm=20, r_deg=90)
    assert not ctx.robot.calls


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


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "hardlink"])
def test_tray_reader_refuses_unsafe_file_before_readable_open(tmp_path, monkeypatch, kind):
    tmp_path.chmod(0o700)
    path = tmp_path / "tray.toml"
    target = tmp_path / "target.toml"
    target.write_text('name = "private"\n')
    target.chmod(0o600)
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "directory":
        path.mkdir(mode=0o700)
    else:
        os.link(target, path)
    original = os.open

    def checked(path, flags, *args, **kwargs):
        assert not str(path).startswith("/proc/self/fd/"), "Unsafe file was opened for reading"
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", checked)
    with pytest.raises((ValueError, OSError)):
        _read_tray(path)


def test_tray_reader_is_bounded_and_does_not_follow_parent_symlinks(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "tray.toml"
    path.write_bytes(b"x" * (16 * 1024 + 1))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="16 KiB"):
        _read_tray(path)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        _read_tray(alias / path.name)
