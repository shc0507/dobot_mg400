"""Offline checks for the XDV authority-bound read interfaces."""

import copy
import gc
import io
import ipaddress
import json
import os
import socket
import sys
import threading
import time
import urllib.error
from collections import deque
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from stage_helpers import home_stage
from test_deploy_mg400ctl import ctl as ctl
from test_orchestrator_runner import FakeService, running, until
from test_orchestrator_transfer import Robot

from labkit.errors import ServiceError
from orchestrator import cli
from orchestrator import runner as runner_module
from orchestrator.app import DemoResources, OrchestratorService, demo_environment
from orchestrator.clients import ThermalAdapter, from_settings
from orchestrator.recipe import validate
from orchestrator.rehearsal import Rehearsal
from orchestrator.runner import Context, Revoked, Runner, StepClient, _Authority
from orchestrator.skills import Registry, default_registry
from orchestrator.skills.crucible import CRUCIBLE_SETTINGS, PREFIX, load, unload
from orchestrator.stations import AXES, StationStore, Workspace
from orchestrator.store import RunStore
from orchestrator.tool import Pose, ToolOffset, flange_to_fingertip


@pytest.fixture(autouse=True)
def crucible_loopback_only(monkeypatch):
    connect = socket.socket.connect

    def guarded(sock, address):
        assert ipaddress.ip_address(address[0]).is_loopback
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture
def crucible_geometry(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    workspace = Workspace(
        {"x_mm": [0, 500], "y_mm": [-300, 300], "z_mm": [0, 250], "r_deg": [-720, 720]}
    )
    for field, axis in AXES.items():
        for index, side in enumerate(("MIN", "MAX")):
            monkeypatch.setenv(
                f"MG400_ORCH_{axis}_{side}_{'DEG' if axis == 'R' else 'MM'}",
                str(workspace.bounds[field][index]),
            )
    stations = StationStore(workspace, tmp_path / "stations.toml")
    stations.save("start", Pose(203, 4, 45, 0), approach_mm=10)
    stations.save(
        "demo-start",
        flange_to_fingertip(Pose(250, 10, 40, 30), ToolOffset(3, 4, 5)),
        approach_mm=10,
    )
    stations.save("slot", Pose(230, 20, 30, 90), approach_mm=20, zone="tray")
    stations.save("crucible-load", Pose(260, -20, 20, -90), approach_mm=20, zone="crucible-load")
    tool = tmp_path / "tool.toml"
    tool.write_text("offset_mm = [3, 4, 5]\n")
    tool.chmod(0o640)
    monkeypatch.setenv("MG400_ORCH_STATIONS_FILE", str(stations.path))
    monkeypatch.setenv("MG400_ORCH_TOOL_FILE", str(tool))
    monkeypatch.setenv("MG400_ORCH_PRE_GRIP", "900")
    for suffix, value in {
        "LOAD_MAX_C": 50,
        "UNLOAD_MAX_C": 40,
        "GRIP": 400,
        "RELEASE": 900,
        "AWAKE_S": 60,
        "MAX_AGE_S": 2,
    }.items():
        monkeypatch.setenv(PREFIX + suffix, str(value))
    return stations


class CrucibleService:
    def __init__(self, name):
        self.name = name
        self.value = dict(
            connected=True,
            busy=False,
            locked=False,
            fault=None,
            lease={"holder": "owned-run", "remaining_s": 2},
        )
        self.calls = []
        self.after = None
        if name == "furnace":
            self.value.update(
                run_state_code=1,
                run_state="STOP/COOLING",
                control_mode="STOPPED",
                sequence={"running": False, "phase": "stopped"},
                sample_time=time.time(),
                pv_c=25,
            )
        elif name == "stage":
            self.value.update(
                feedback_fresh=True,
                feedback_age_s=0,
                automation={
                    "configured": True,
                    "active": None,
                    "error": None,
                    "at_poses": {
                        "furnace": {
                            "matched": False,
                            "axes": {"bottom": True, "top": True, "insert": False},
                        }
                    },
                },
                motors=[
                    dict(
                        id=motor,
                        homed=True,
                        automation_ready=True,
                        soft=True,
                        mode="idle",
                        position_mm=0,
                        cooldown=0,
                        available=True,
                        awake=False,
                        at_positions=["load"] if motor == "insert" else [],
                    )
                    for motor in ("bottom", "top", "insert")
                ],
            )
        else:
            self.value.update(state="streaming", frame_age_s=0, frame_sequence=1)
        self.reading = dict(
            name="crucible",
            x0=10,
            y0=10,
            x1=20,
            y1=20,
            min_c=20,
            mean_c=21,
            max_c=22,
            frame_sequence=1,
            frame_age_s=0,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self.before_roi = None

    def state(self):
        return copy.deepcopy(self.value)

    def owned(self, state):
        return state.get("lease", {}).get("holder") == "owned-run"

    def describe(self):
        return {
            "max_frame_age_s": 3,
            "regions": {"crucible": {"x0": 10, "y0": 10, "x1": 20, "y1": 20}},
        }

    def roi(self, name):
        self.calls.append(("roi", name))
        if self.before_roi:
            self.before_roi()
        return copy.deepcopy(self.reading)

    def command(self, action, **params):
        self.calls.append((action, params))
        insert = self.value["motors"][2]
        if action == "awake":
            insert["awake"] = True
        else:
            assert action == "goto" and params["motor"] == "insert"
            insert["at_positions"] = [params["name"]]
            proof = self.value["automation"]["at_poses"]["furnace"]
            proof["matched"] = proof["axes"]["insert"] = params["name"] == "furnace"
        identity = f"stage-{len(self.calls)}"
        self.value["last_command"] = dict(
            id=identity,
            action=action,
            client="owned-run",
            result="ok",
            finished_at=time.time(),
        )
        if self.after:
            self.after(action)
        return {"command_id": identity}


class CrucibleContext:
    def __init__(self, *, unloading=False):
        self.clients = {"robot": Robot()} | {
            name: CrucibleService(name) for name in ("stage", "furnace", "thermal")
        }
        self.zones = self
        self.events, self.held, self.retained = [], set(), set()
        self.definitions = {
            "tray": {"owner": "robot", "conditions": []},
            "crucible-load": {"owner": "robot", "conditions": ["stage_at_load"]},
            "furnace-tube": {"owner": "stage", "conditions": ["robot_clear"]},
        }
        self.remaining = 30
        self.sleeps = 0
        self.on_sleep = None
        if unloading:
            stage = self.clients["stage"].value
            stage["motors"][2]["at_positions"] = ["furnace"]
            proof = stage["automation"]["at_poses"]["furnace"]
            proof["matched"] = proof["axes"]["insert"] = True

    def check(self):
        if self.remaining <= 0:
            raise TimeoutError("step deadline")

    def sleep(self, seconds):
        self.sleeps += 1
        if self.on_sleep:
            self.on_sleep()
        self.check()

    def remaining_s(self):
        self.check()
        return self.remaining

    def zone_definitions(self):
        return copy.deepcopy(self.definitions)

    @contextmanager
    def guard(self, check):
        check()
        yield
        check()

    @contextmanager
    def take(self, name):
        self.events.append(("enter", name, len(self.clients["robot"].calls)))
        self.held.add(name)
        try:
            yield
        except BaseException:
            self.retained.add(name)
            self.events.append(("unknown", name))
            raise
        else:
            self.retained.discard(name)
            self.events.append(("leave", name, len(self.clients["robot"].calls)))
        finally:
            self.held.remove(name)

    def declare(self, name):
        self.events.append(("declare", name, len(self.clients["robot"].calls)))
        self.held.add(name)
        return name

    def finish(self, name, *, exceptional=False):
        self.held.remove(name)
        self.retained.add(name)
        self.events.append(("unknown" if exceptional else "retained", name))


def test_load_departs_robot_zone_before_insert_and_retains_tube(crucible_geometry):
    ctx = CrucibleContext()
    stage = ctx.clients["stage"]

    def check(action):
        assert "crucible-load" not in ctx.held
        if action == "goto":
            assert "furnace-tube" in ctx.held
            assert ctx.clients["robot"].value["gripper"]["outcome"] == "arrived"

    stage.after = check
    load(ctx, "slot")
    assert stage.calls == [
        ("awake", {"motor": "insert", "seconds": 60}),
        ("goto", {"motor": "insert", "name": "furnace"}),
    ]
    assert ctx.retained == {"furnace-tube"}
    assert ctx.events[-1] == ("retained", "furnace-tube")
    assert all(
        params["speed"] == 1 for action, params in ctx.clients["robot"].calls if action == "execute"
    )
    assert ctx.clients["furnace"].calls == []


def test_unload_measures_load_departure_then_waits_for_named_roi(crucible_geometry):
    ctx = CrucibleContext(unloading=True)
    thermal = ctx.clients["thermal"]
    thermal.reading["max_c"] = 80

    def cool():
        assert not ctx.clients["robot"].calls
        assert not ctx.held and not ctx.retained
        thermal.reading["max_c"] = 25

    ctx.on_sleep = cool
    unload(ctx, "slot")
    assert ctx.sleeps == 1
    assert ctx.clients["stage"].calls == [
        ("goto", {"motor": "insert", "name": "load"}),
        ("awake", {"motor": "insert", "seconds": 60}),
    ]
    assert ctx.events[:2] == [("enter", "furnace-tube", 0), ("leave", "furnace-tube", 0)]
    assert all(call == ("roi", "crucible") for call in thermal.calls)
    assert not ctx.retained


@pytest.mark.parametrize("suffix", ["GRIP", "RELEASE", "LOAD_MAX_C", "AWAKE_S", "MAX_AGE_S"])
def test_missing_settings_never_dispatch(crucible_geometry, monkeypatch, suffix):
    monkeypatch.delenv(PREFIX + suffix)
    ctx = CrucibleContext()
    with pytest.raises(ValueError):
        load(ctx, "slot")
    assert not ctx.clients["robot"].calls and not ctx.clients["stage"].calls


@pytest.mark.parametrize(
    "params",
    [
        {"grip": True},
        {"release": 399},
        {"load_max_c": float("nan")},
        {"load_max_c": float("inf")},
        {"awake_s": 60.001},
        {"awake_s": 0},
        {"max_age_s": -2},
        {"max_age_s": True},
        {"awake_s": 29},
    ],
)
def test_invalid_or_insufficient_budget_never_dispatch(crucible_geometry, params):
    ctx = CrucibleContext()
    with pytest.raises(ValueError):
        load(ctx, "slot", **params)
    assert not ctx.clients["robot"].calls and not ctx.clients["stage"].calls


@pytest.mark.parametrize("name", ["crucible-load", "missing"])
def test_invalid_slot_fails_before_stage_motion(crucible_geometry, name):
    ctx = CrucibleContext(unloading=True)
    with pytest.raises(ValueError):
        unload(ctx, name)
    assert not ctx.clients["robot"].calls and not ctx.clients["stage"].calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_state_code", 0),
        ("run_state_code", True),
        ("run_state", "RUN"),
        ("locked", True),
        ("fault", {"message": "unknown"}),
        ("pv_c", 50),
        ("pv_c", float("nan")),
        ("sample_time", 0),
        ("sample_time", float("inf")),
        ("sample_time", 1),
        ("sample_time", 1e20),
        ("connected", False),
        ("sequence", {"running": True}),
        ("persistence_error", "disk full"),
    ],
)
def test_furnace_refusal_prevents_all_dispatch(crucible_geometry, field, value):
    ctx = CrucibleContext()
    ctx.clients["furnace"].value[field] = value
    with pytest.raises((RuntimeError, ValueError)):
        load(ctx, "slot")
    assert not ctx.clients["robot"].calls and not ctx.clients["stage"].calls


@pytest.mark.parametrize(
    "mutation",
    [
        lambda state: state.update(feedback_fresh=False),
        lambda state: state.update(feedback_age_s=3),
        lambda state: state.update(busy=True),
        lambda state: state["automation"].update(active={"action": "pose"}),
        lambda state: state["automation"].update(at_poses={}),
        lambda state: state["automation"]["at_poses"]["furnace"]["axes"].update(bottom=False),
        lambda state: state["motors"][0].update(homed=False),
        lambda state: state["motors"][1].update(soft=False),
        lambda state: state["motors"][2].update(automation_ready=False),
        lambda state: state["motors"][2].update(at_positions=[]),
    ],
)
def test_stage_refusal_prevents_all_dispatch(crucible_geometry, mutation):
    ctx = CrucibleContext()
    mutation(ctx.clients["stage"].value)
    with pytest.raises(RuntimeError):
        load(ctx, "slot")
    assert not ctx.clients["robot"].calls and not ctx.clients["stage"].calls


def test_unload_refuses_partial_pose_without_commanding_pose(crucible_geometry):
    ctx = CrucibleContext()
    with pytest.raises(RuntimeError, match="complete furnace pose"):
        unload(ctx, "slot")
    assert ctx.clients["stage"].calls == []


def test_waypoint_and_zone_preflight_precedes_unload_stage_motion(crucible_geometry, monkeypatch):
    ctx = CrucibleContext(unloading=True)
    del ctx.definitions["tray"]
    with pytest.raises(ValueError, match="robot"):
        unload(ctx, "slot")
    assert ctx.clients["stage"].calls == []
    ctx = CrucibleContext(unloading=True)
    monkeypatch.setenv("MG400_ORCH_Z_MAX_MM", "39")
    with pytest.raises(ValueError, match="approach"):
        unload(ctx, "slot")
    assert ctx.clients["stage"].calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "whole-frame"),
        ("max_c", float("nan")),
        ("min_c", float("inf")),
        ("frame_age_s", -1),
        ("frame_age_s", float("nan")),
        ("frame_age_s", 4),
        ("frame_sequence", 0),
        ("frame_sequence", True),
        ("timestamp", "2020-01-01T00:00:00+00:00"),
        ("timestamp", "2999-01-01T00:00:00+00:00"),
        ("timestamp", "2026-01-01T00:00:00"),
    ],
)
def test_bad_roi_never_starts_robot_and_never_uses_fallback(crucible_geometry, field, value):
    ctx = CrucibleContext(unloading=True)
    ctx.clients["thermal"].reading[field] = value
    with pytest.raises((RuntimeError, ValueError)):
        unload(ctx, "slot")
    assert ctx.clients["robot"].calls == []
    assert ctx.clients["thermal"].calls == [("roi", "crucible")]


@pytest.mark.parametrize(
    "bounds",
    [
        None,
        {},
        {"x0": 10, "y0": 10, "x1": 20},
        {"x0": True, "y0": 10, "x1": 20, "y1": 20},
        {"x0": 10.0, "y0": 10, "x1": 20, "y1": 20},
        {"x0": -1, "y0": 10, "x1": 20, "y1": 20},
        {"x0": 10, "y0": 10, "x1": 10, "y1": 20},
        {"x0": 10, "y0": 10, "x1": 161, "y1": 20},
        {"x0": 10, "y0": 10, "x1": 20, "y1": 121},
    ],
)
def test_invalid_advertised_crucible_geometry_prevents_all_actuation(crucible_geometry, bounds):
    ctx = CrucibleContext(unloading=True)
    ctx.clients["thermal"].describe = lambda: {
        "max_frame_age_s": 3,
        "regions": {"crucible": bounds},
    }
    with pytest.raises(ValueError, match="region"):
        unload(ctx, "slot")
    assert not ctx.clients["stage"].calls and not ctx.clients["robot"].calls


@pytest.mark.parametrize(
    "bounds",
    [
        {},
        {"x0": 0, "y0": 0, "x1": 1, "y1": 1},
        {"x0": False, "y0": 10, "x1": 20, "y1": 20},
        {"x0": 10, "y0": 10, "x1": 20.0, "y1": 20},
        {"x0": 10, "y0": 10, "x1": 160, "y1": 120},
    ],
)
def test_reading_must_match_pinned_named_crucible_rectangle(crucible_geometry, bounds):
    ctx = CrucibleContext(unloading=True)
    reading = ctx.clients["thermal"].reading
    for key in ("x0", "y0", "x1", "y1"):
        reading.pop(key)
    reading.update(bounds)
    with pytest.raises((ValueError, RuntimeError), match="region"):
        unload(ctx, "slot")
    assert ctx.clients["robot"].calls == []
    assert ctx.clients["thermal"].calls == [("roi", "crucible")]


def test_starting_pose_is_rechecked_after_cooldown_before_first_goto(crucible_geometry):
    ctx = CrucibleContext(unloading=True)
    stage = ctx.clients["stage"].value
    insert = stage["motors"][2]
    insert.update(available=False, cooldown=0.5)

    def changed():
        insert.update(available=True, cooldown=0, at_positions=[])
        stage["automation"]["at_poses"]["furnace"]["matched"] = False

    ctx.on_sleep = changed
    with pytest.raises(RuntimeError, match="starting position"):
        unload(ctx, "slot")
    assert not ctx.clients["stage"].calls and not ctx.clients["robot"].calls
    assert ctx.retained == {"furnace-tube"}


def test_hot_roi_timeout_never_retries_motion_or_extends_awake(crucible_geometry):
    ctx = CrucibleContext(unloading=True)
    ctx.clients["thermal"].reading["max_c"] = 100
    ctx.on_sleep = lambda: setattr(ctx, "remaining", 0)
    with pytest.raises(TimeoutError):
        unload(ctx, "slot")
    assert ctx.clients["robot"].calls == []
    assert len(ctx.clients["stage"].calls) == 2


def test_awake_loss_after_wait_prevents_grasp(crucible_geometry):
    ctx = CrucibleContext(unloading=True)
    ctx.clients["thermal"].reading["max_c"] = 100

    def lost():
        ctx.clients["stage"].value["motors"][2]["awake"] = False

    ctx.on_sleep = lost
    with pytest.raises(RuntimeError, match="awake interval"):
        unload(ctx, "slot")
    assert ctx.clients["robot"].calls == []
    assert len(ctx.clients["stage"].calls) == 2


@pytest.mark.parametrize("service", ["stage", "furnace", "thermal"])
def test_ownership_loss_during_roi_refuses_pick(crucible_geometry, service):
    ctx = CrucibleContext(unloading=True)
    ctx.clients["thermal"].before_roi = lambda: (
        ctx.clients[service].value["lease"].update(holder="other")
    )
    with pytest.raises(RuntimeError, match="owned"):
        unload(ctx, "slot")
    assert ctx.clients["robot"].calls == []


@pytest.mark.parametrize("failure", ["empty", "dropped", "wrong-arrival"])
def test_failed_robot_transfer_never_inserts(crucible_geometry, failure):
    ctx = CrucibleContext()
    robot = ctx.clients["robot"]
    if failure == "empty":
        robot.empty = True
    else:

        def fail(action):
            if action == "execute":
                if failure == "dropped":
                    robot.value["gripper"]["outcome"] = "dropped"
                else:
                    robot.value["actual"][0] += 1

        robot.after = fail
    with pytest.raises(RuntimeError):
        load(ctx, "slot")
    assert all(action != "goto" for action, _ in ctx.clients["stage"].calls)


def test_unknown_stage_outcome_retains_tube_without_retry(crucible_geometry):
    ctx = CrucibleContext(unloading=True)
    ctx.clients["stage"].command = lambda *args, **kwargs: {}
    with pytest.raises(RuntimeError, match="unknown"):
        unload(ctx, "slot")
    assert ctx.retained == {"furnace-tube"} and ctx.clients["robot"].calls == []


def test_crucible_registry_keeps_full_scalar_and_unknown_parameter_validation():
    registry = default_registry()
    assert registry["crucible.load"].validate({"from_slot": "slot"})["load_max_c"] == -1
    assert registry["crucible.unload"].services == ("robot", "stage", "furnace", "thermal")
    for params in ({"from_slot": "slot", "grip": True}, {"from_slot": "slot", "retry": True}):
        with pytest.raises(ValueError):
            registry["crucible.load"].validate(params)


def test_stage_pose_proof_uses_all_calibrated_axes_and_actual_feedback(stage_rig):
    controller, board, clock = stage_rig
    home_stage(controller, clock)
    proof = controller.snapshot()["automation"]["at_poses"]["furnace"]
    assert proof == {"matched": False, "axes": {"bottom": True, "top": True, "insert": False}}
    controller.pose("furnace")
    for _ in range(30):
        clock.advance(0.5)
        controller.poll()
        if not controller.snapshot()["busy"]:
            break
        assert controller.snapshot()["automation"]["at_poses"] == {}
    assert controller.snapshot()["automation"]["at_poses"]["furnace"]["matched"]
    clock.advance(2.01)
    assert controller.snapshot()["automation"]["at_poses"] == {}
    controller.poll()
    assert controller.snapshot()["automation"]["at_poses"]["furnace"]["matched"]
    controller.set_microstep(2)
    assert controller.snapshot()["automation"]["at_poses"] == {}


@pytest.mark.parametrize("failure", ["reference", "soft", "active", "fault", "disconnect"])
def test_stage_pose_proof_disappears_on_any_uncertainty(stage_rig, failure):
    controller, board, clock = stage_rig
    home_stage(controller, clock)
    if failure == "reference":
        controller._references.pop("bottom")
    elif failure == "soft":
        board.motors["top"].soft = False
        controller.poll()
    elif failure == "active":
        controller._automation = dict(
            action="pose", motor=None, pose="furnace", phase="cooldown", axes=("insert",)
        )
    elif failure == "fault":
        controller._contract_fault = "unknown stop"
    else:
        controller.disconnect()
    assert controller.snapshot()["automation"]["at_poses"] == {}


def test_pose_proof_uses_nonzero_calibration_and_current_microstep(stage_rig):
    controller, board, clock = stage_rig
    controller._geometry = replace(
        controller._geometry,
        poses={"furnace": (("bottom", 3), ("top", 5), ("insert", 20))},
    )
    for mode in (1, 4):
        if mode != 1:
            controller.set_microstep(mode)
        home_stage(controller, clock)
        proof = controller.snapshot()["automation"]["at_poses"]["furnace"]
        assert proof == {"matched": False, "axes": {"bottom": False, "top": False, "insert": False}}
        for motor, mm in controller._geometry.poses["furnace"]:
            board.motors[motor].position = controller._geometry.motors[motor].target(mm, mode)
        controller.poll()
        assert controller.snapshot()["automation"]["at_poses"]["furnace"]["matched"] is True


def test_load_accepts_fresh_stopped_cooling_state_without_starting_outputs(crucible_geometry):
    ctx = CrucibleContext()
    furnace = ctx.clients["furnace"]
    furnace.value["busy"] = True
    furnace.value["sequence"]["phase"] = "cooling"
    load(ctx, "slot")
    assert furnace.calls == []


def test_cooldown_wait_precedes_single_dispatch_and_rechecks_furnace(crucible_geometry):
    ctx = CrucibleContext(unloading=True)
    insert = ctx.clients["stage"].value["motors"][2]
    insert.update(available=False, cooldown=0.5)

    def ready():
        assert ctx.clients["stage"].calls == []
        insert.update(available=True, cooldown=0)

    ctx.on_sleep = ready
    unload(ctx, "slot")
    assert ctx.sleeps == 1
    assert [action for action, _ in ctx.clients["stage"].calls] == ["goto", "awake"]


@pytest.mark.parametrize("root", [True, False])
@pytest.mark.parametrize("command", ["run", "rehearse", "runs", "station"])
def test_installed_crucible_settings_keep_exact_command_and_identity_boundaries(
    ctl,
    monkeypatch,
    root,
    command,
):
    production = {key: f"host-{key}" for key in CRUCIBLE_SETTINGS}
    caller = {key: f"caller-{key}" for key in CRUCIBLE_SETTINGS}
    forbidden = {"MG400_ORCH_CRUCIBLE_DEVICE", "MG400_FURNACE_SERIAL", "HTTP_PROXY", "PYTHONPATH"}
    for key, value in caller.items():
        monkeypatch.setenv(key, value)
    for key in forbidden:
        monkeypatch.setenv(key, "forbidden")
    if root and command != "rehearse":
        ctl.host.settings.write_text(
            "\n".join(f"{key}={value}" for key, value in production.items())
        )
    else:
        ctl.host.settings.unlink()
    if not root:
        for name in ("getuid", "geteuid", "getgid", "getegid"):
            monkeypatch.setattr(ctl.module.os, name, lambda: 1000)
    arguments = ["list"] if command == "station" else ["recipe-or-run-id"]
    assert ctl.call(command, *arguments) == 0
    program, argv, environment = ctl.execvpe.call_args.args
    expected = production if root and command == "run" else caller
    if command not in {"run", "rehearse"}:
        expected = {}
    assert {
        key: value for key, value in environment.items() if key in CRUCIBLE_SETTINGS
    } == expected
    assert forbidden.isdisjoint(environment)
    python = str(ctl.host.code / ".venv/bin/python")
    assert program == ("/usr/sbin/runuser" if root else python)
    assert argv[-6:] == [python, "-I", "-m", "orchestrator", command, *arguments]
    ctl.run.assert_not_called()


@pytest.mark.parametrize("axis", ["bottom", "top"])
def test_changed_alignment_after_preflight_prevents_robot_and_insert(crucible_geometry, axis):
    ctx = CrucibleContext()
    stage = ctx.clients["stage"]

    def change(action):
        if action == "awake":
            stage.value["automation"]["at_poses"]["furnace"]["axes"][axis] = False
            next(motor for motor in stage.value["motors"] if motor["id"] == axis)["position_mm"] = 1

    stage.after = change
    with pytest.raises(RuntimeError, match="alignment"):
        load(ctx, "slot")
    assert not ctx.clients["robot"].calls
    assert [action for action, _ in stage.calls] == ["awake"]


def test_demo_environments_retain_only_explicit_crucible_data(monkeypatch):
    explicit = {key: f"explicit-{key}" for key in CRUCIBLE_SETTINGS}
    forbidden = {
        "MG400_ORCH_FURNACE_URL",
        "MG400_ORCH_FURNACE_TOKEN",
        "MG400_ORCH_THERMAL_URL",
        "MG400_ORCH_THERMAL_TOKEN",
        "MG400_FURNACE_SERIAL",
        "MG400_THERMAL_ROI_FILE",
        "MG400_ORCH_ZONE_STATE_FILE",
        "HTTP_PROXY",
        "https_proxy",
        "PYTHONPATH",
        "MG400_ORCH_CRUCIBLE_DEVICE",
    }
    for key, value in explicit.items():
        monkeypatch.setenv(key, value)
    for key in forbidden:
        monkeypatch.setenv(key, "production-must-not-be-used")
    with cli._demo_environment():
        assert forbidden.isdisjoint(os.environ)
        assert {key: os.environ[key] for key in explicit} == explicit
    clean = demo_environment({**os.environ, "MG400_ORCH_TOKEN": "frontend-token"})
    assert forbidden.isdisjoint(clean)
    assert {key: clean[key] for key in explicit} == explicit
    assert clean["MG400_ORCH_TOKEN"] == "frontend-token"


def _crucible_files(tmp_path, monkeypatch):
    config = tmp_path / "crucible-zones.toml"
    config.write_text("""[zones.tray]
owner = "robot"
conditions = []
[zones.crucible-load]
owner = "robot"
conditions = ["stage_at_load"]
[zones.furnace-tube]
owner = "stage"
conditions = ["robot_clear"]
""")
    config.chmod(0o640)
    monkeypatch.setenv("MG400_ORCH_ZONES_FILE", str(config))
    regions = tmp_path / "regions.toml"
    regions.write_text("[regions.crucible]\nx0=10\ny0=10\nx1=20\ny1=20\n")
    regions.chmod(0o640)
    monkeypatch.setenv(PREFIX + "ROIS_FILE", str(regions))
    return config


def _home_owned_demo(stage):
    stage.acquire()
    try:
        for motor in ("bottom", "top", "insert"):
            command = stage.command("home", motor=motor)["command_id"]
            deadline = time.monotonic() + 5
            while True:
                stage.heartbeat()
                state = stage.state()
                axis = next(item for item in state["motors"] if item["id"] == motor)
                result = state.get("last_command")
                if result and result["id"] == command:
                    assert result["result"] == "ok", result
                    if axis["available"]:
                        break
                assert time.monotonic() < deadline, state
                time.sleep(0.03)
    finally:
        stage.release()


@pytest.fixture
def crucible_gc_observer():
    """Observe GC pauses only during this test, without changing collection policy."""
    clock = time.monotonic
    starts = [None, None, None]
    counts = [0, 0, 0]
    maximum = [None, None, None]
    recent = deque(maxlen=8)

    def observe(phase, info):
        generation = info["generation"]
        sampled_at = clock()
        if phase == "start":
            starts[generation] = sampled_at
        elif phase == "stop" and starts[generation] is not None:
            started_at = starts[generation]
            starts[generation] = None
            event = (
                generation,
                started_at,
                sampled_at,
                sampled_at - started_at,
                info.get("collected", 0),
                info.get("uncollectable", 0),
            )
            if generation == 2:
                recent.append(event)
            counts[generation] += 1
            if maximum[generation] is None or event[3] > maximum[generation][3]:
                maximum[generation] = event

    def snapshot():
        # Copy bounded tuples before allocating dictionaries that can themselves trigger GC.
        latest, maxima = tuple(recent), tuple(maximum)
        fields = (
            "generation",
            "started_monotonic",
            "stopped_monotonic",
            "duration_s",
            "collected",
            "uncollectable",
        )
        return {
            "captured_monotonic": clock(),
            "enabled": gc.isenabled(),
            "thresholds": list(gc.get_threshold()),
            "counts": list(counts),
            "active_starts": list(starts),
            "recent_generation_2": [dict(zip(fields, event)) for event in latest],
            "maximum_by_generation": [
                dict(zip(fields, event)) if event else None for event in maxima
            ],
        }

    gc.callbacks.append(observe)
    try:
        yield snapshot
    finally:
        # Other observers can be installed during the test. Remove only ours.
        if observe in gc.callbacks:
            gc.callbacks.remove(observe)


def _robot_failure_sample(robot):
    """Read only bounded local state; never query the demo's sockets or clients."""
    with robot._lock:
        sampled_at = time.monotonic()
        feedback = robot._sample
        return {
            "message": str(robot._message)[:400],
            "locked": robot._locked,
            "fault": robot._fault,
            "syncing": robot._syncing,
            "busy": robot._busy,
            "owner_present": robot._owner is not None,
            "mode": feedback.mode if feedback is not None else None,
            "error_status": feedback.error_status if feedback is not None else None,
            "feedback_age_s": sampled_at - robot._received if robot._received else None,
            "lease_renewal_age_s": sampled_at - robot._heartbeat if robot._heartbeat else None,
        }


def _feedback_reader_failure_stack(robot):
    """Inspect only the first failure, without instrumenting successful reads."""
    frames = sys._current_frames()
    frame = frames.get(robot._reader.ident) if robot._reader is not None else None
    records = []
    try:
        while frame is not None and len(records) < 8:
            record = {
                "file": os.path.basename(frame.f_code.co_filename)[:100],
                "function": frame.f_code.co_name[:80],
                "line": frame.f_lineno,
            }
            if (
                frame.f_code.co_name == "read_latest"
                and frame.f_globals.get("__name__") == "mg400.feedback"
            ):
                local = frame.f_locals
                connection = local.get("self")
                buffer = vars(connection).get("_buffer") if connection is not None else None
                timeout = vars(connection).get("timeout_s") if connection is not None else None
                deadline = local.get("deadline")
                drains = local.get("drain_reads")
                record.update(
                    drain_reads=drains if type(drains) is int else None,
                    latest_present=local.get("latest") is not None,
                    deadline_remaining_s=(
                        deadline - time.monotonic() if type(deadline) in (int, float) else None
                    ),
                    buffered_bytes=len(buffer) if isinstance(buffer, (bytes, bytearray)) else None,
                    timeout_s=timeout if type(timeout) in (int, float) else None,
                )
            records.append(record)
            frame = frame.f_back
        return {"frames": records}
    except Exception as error:
        return {"frames": records, "observer_error": type(error).__name__}
    finally:
        # Never retain live frames or their locals in the failure records.
        del frame
        frames.clear()


def _robot_operation_exception(error=None):
    """Copy the active operation's traceback without retaining exception objects."""
    if error is None:
        error_type, error, trace = sys.exc_info()
    else:
        error_type, trace = type(error), error.__traceback__
    records, traversed = [], 0
    try:
        while trace is not None and traversed < 32:
            frame = trace.tb_frame
            record = {
                "file": os.path.basename(frame.f_code.co_filename)[:100],
                "function": frame.f_code.co_name[:80],
                "line": trace.tb_lineno,
            }
            if (
                frame.f_code.co_name == "_fresh"
                and frame.f_globals.get("__name__") == "mg400.joint_control"
            ):
                local = frame.f_locals
                age, received = local.get("age"), local.get("received")
                allowed = local.get("allowed_modes")
                sample = local.get("sample")
                sample_mode = getattr(sample, "mode", None)
                sample_error = getattr(sample, "error_status", None)
                record.update(
                    age_s=age if type(age) in (int, float) else None,
                    received_monotonic=received if type(received) in (int, float) else None,
                    allowed_modes=(
                        sorted(allowed)
                        if isinstance(allowed, (set, frozenset, list, tuple))
                        and len(allowed) <= 8
                        and all(type(mode) is int for mode in allowed)
                        else None
                    ),
                    sample_mode=sample_mode if type(sample_mode) is int else None,
                    sample_error_status=sample_error if type(sample_error) is bool else None,
                )
            records.append(record)
            records = records[-8:]
            traversed += 1
            trace = trace.tb_next
        return {
            "type": error_type.__name__[:80] if error_type is not None else None,
            "captured_monotonic": time.monotonic(),
            "frames": records,
            "truncated": traversed > 8 or trace is not None,
        }
    except Exception as observer_error:
        return {"frames": records, "observer_error": type(observer_error).__name__}
    finally:
        # The result contains only primitive values, never traceback/frame locals.
        del error, trace


def _observe_robot_operation_failures(robot, diagnostics, gc_snapshot):
    """Observe the first rejected operation before its internal stop clears state."""
    control = robot._control
    original = control._failed
    had_override = "_failed" in vars(control)
    previous_override = vars(control).get("_failed")

    def observed(*args, **kwargs):
        try:
            if "first_operation_failure" not in diagnostics:
                error = args[0] if args else kwargs.get("error")
                record = {
                    "caller": sys._getframe(1).f_code.co_name[:80],
                    "operation_exception": _robot_operation_exception(error),
                    "feedback_reader": _feedback_reader_failure_stack(robot),
                    "gc": gc_snapshot(),
                }
                # Never wait for the publication lock before the original stop.
                if robot._lock.acquire(blocking=False):
                    try:
                        record.update(_robot_failure_sample(robot), panel_lock_busy=False)
                    finally:
                        robot._lock.release()
                else:
                    record["panel_lock_busy"] = True
                diagnostics["first_operation_failure"] = record
        except Exception as observer_error:
            diagnostics["operation_observer_error"] = type(observer_error).__name__
        return original(*args, **kwargs)

    def restore():
        if had_override:
            control._failed = previous_override
        else:
            del control._failed

    control._failed = observed
    return restore


def _observe_robot_stops(robot, diagnostics, gc_snapshot):
    """Preserve the initiating evidence before stop workers or cleanup replace it."""
    original = robot._request_stop
    had_override = "_request_stop" in vars(robot)
    previous_override = vars(robot).get("_request_stop")

    def observed(**kwargs):
        try:
            with robot._lock:
                diagnostics["stop_count"] += 1
                reason = kwargs.get("reason")
                if len(diagnostics["stops"]) < 8 or (
                    reason is not None and diagnostics["first_reason"] is None
                ):
                    record = {
                        **_robot_failure_sample(robot),
                        "caller": sys._getframe(1).f_code.co_name[:80],
                        "reason": str(reason)[:400] if reason is not None else None,
                        "disabling": bool(kwargs.get("disabling", False)),
                        "disconnect": bool(kwargs.get("disconnect", False)),
                    }
                    if diagnostics["stop_count"] == 1:
                        record["operation_exception"] = _robot_operation_exception()
                        record["feedback_reader"] = _feedback_reader_failure_stack(robot)
                        record["gc"] = gc_snapshot()
                    if len(diagnostics["stops"]) < 8:
                        diagnostics["stops"].append(record)
                    if reason is not None and diagnostics["first_reason"] is None:
                        diagnostics["first_reason"] = record
        except Exception as error:
            # Observation must not replace or suppress the real stop operation.
            diagnostics["observer_error"] = type(error).__name__
        return original(**kwargs)

    def restore():
        with robot._lock:
            if had_override:
                robot._request_stop = previous_override
            else:
                del robot._request_stop

    robot._request_stop = observed
    return restore


@pytest.mark.parametrize("scenario", ["complete", "unhomed", "hot"])
def test_public_rehearsal_load_unload_uses_owned_four_services_only(
    crucible_geometry,
    tmp_path,
    monkeypatch,
    scenario,
    crucible_gc_observer,
):
    _crucible_files(tmp_path, monkeypatch)
    forbidden = {
        "MG400_ORCH_FURNACE_URL",
        "MG400_ORCH_FURNACE_TOKEN",
        "MG400_ORCH_THERMAL_URL",
        "MG400_ORCH_THERMAL_TOKEN",
        "MG400_ORCH_ZONE_STATE_FILE",
        "MG400_FURNACE_SERIAL",
        "MG400_GRIPPER_FEEDBACK",
        "HTTP_PROXY",
        "ALL_PROXY",
        "PYTHONPATH",
    }
    for key in forbidden:
        monkeypatch.setenv(key, "production-must-not-be-used")
    demos = []
    robot_stops = {"stop_count": 0, "stops": [], "first_reason": None}

    def diagnostics():
        try:
            current = _robot_failure_sample(demos[0].robot_state) if demos else None
            data = json.dumps(
                {**robot_stops, "current": current, "gc": crucible_gc_observer()}, sort_keys=True
            )
            (tmp_path / "robot-failure.json").write_text(data, encoding="utf-8")
            report = "robot_diagnostics=" + data
            # Assertion tuple reprs can be truncated by pytest. Captured output
            # and this fixture sidecar preserve the complete bounded evidence.
            print(report)
            return report
        except Exception as error:
            return f"robot_diagnostics_unavailable={type(error).__name__}; {robot_stops!r}"

    class Demo(Rehearsal):
        def __enter__(self):
            assert forbidden.isdisjoint(os.environ), diagnostics()
            super().__enter__()
            demos.append(self)
            # Restore the instance method before resource teardown. Earlier stop
            # records survive runner cleanup and the service's final message.
            self._stack.callback(
                _observe_robot_stops(self.robot_state, robot_stops, crucible_gc_observer)
            )
            self._stack.callback(
                _observe_robot_operation_failures(
                    self.robot_state, robot_stops, crucible_gc_observer
                )
            )
            assert set(self.clients) == {"robot", "stage", "thermal", "furnace"}, diagnostics()
            assert all(client.describe()["demo"] for client in self.clients.values()), diagnostics()
            if scenario != "unhomed":
                _home_owned_demo(self.clients["stage"])

            def answer(request):
                contact = request["position"] == 400
                return {
                    "init": 1,
                    "state": 2 if contact else 1,
                    "position": 600 if contact else request["position"],
                }

            self.simulator.collector.answer = answer
            return self

    monkeypatch.setattr(cli, "Rehearsal", Demo)
    recipe = tmp_path / "crucible.toml"
    recipe.write_text(
        """name = "Owned crucible rehearsal"
version = 1
params = {}
[[steps]]
skill = "robot.enable"
params = {}
timeout_s = 5
[[steps]]
skill = "crucible.load"
params = {from_slot="slot"}
timeout_s = 20
[[steps]]
skill = "crucible.unload"
params = {to_slot="slot", unload_max_c="""
        + ("1" if scenario == "hot" else "40")
        + """}
timeout_s = """
        + ("2" if scenario == "hot" else "20")
        + "\n"
    )
    output = io.StringIO()
    # Clear prior-suite cycles before owned services start their real 250 ms watchdog.
    started = time.monotonic()
    collected = gc.collect()
    robot_stops["gc_preflight"] = {
        "started_monotonic": started,
        "completed_monotonic": time.monotonic(),
        "collected": collected,
    }
    code = cli.main(
        ["rehearse", str(recipe), "--runs-dir", str(tmp_path / "runs")],
        out=output,
        read=Mock(side_effect=EOFError()),
    )
    reports = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith("{")]
    assert reports, (output.getvalue(), diagnostics())
    if scenario == "complete":
        assert code == 0, (
            [(item["status"], item["step"], item["detail"]) for item in reports],
            diagnostics(),
        )
        assert reports[-1]["status"] == "completed" and reports[-1]["zones"] == {}, diagnostics()
        assert demos[0].simulator.collector.targets("answered").count(400) == 2, diagnostics()
    else:
        assert code == 1 and reports[-1]["status"] == "aborted", (output.getvalue(), diagnostics())
        assert any(report["status"] == "held" for report in reports), diagnostics()
        targets = demos[0].simulator.collector.targets("answered")
        assert targets.count(400) == (1 if scenario == "hot" else 0), diagnostics()
        if scenario == "hot":
            held = next(item for item in reports if item["status"] == "held")
            assert any(
                word in held["detail"] for word in ("timed out", "permission", "deadline")
            ), diagnostics()


def test_service_demo_factory_returns_context_bound_furnace_and_roi_clients(
    tmp_path,
    monkeypatch,
):
    _crucible_files(tmp_path, monkeypatch)
    resources = DemoResources(tmp_path / "resources")
    try:
        clients = resources.clients(("furnace", "thermal"), "owned-first")
        assert all(client.describe()["demo"] for client in clients.values())
        assert clients["furnace"].state()["run_state"] == "STOP/COOLING"
        deadline = time.monotonic() + 2
        while clients["thermal"].state().get("frame_sequence") is None:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert clients["thermal"].roi("crucible")["name"] == "crucible"
        fresh = resources.clients(("furnace", "thermal"), "owned-second")
        assert fresh["furnace"].state()["lease"]["holder"] is None
        assert fresh["thermal"].roi("crucible")["name"] == "crucible"
    finally:
        resources.close()
    for client, method, args in (
        (clients["thermal"], "roi", ("crucible",)),
        (fresh["furnace"], "state", ()),
    ):
        with pytest.raises(RuntimeError, match="no longer active"):
            getattr(client, method)(*args)


def test_service_preflight_includes_owned_furnace_for_crucible_recipe(
    crucible_geometry,
    tmp_path,
    monkeypatch,
):
    config = _crucible_files(tmp_path, monkeypatch)
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "crucible.toml").write_text("""name="Crucible"
version=1
params={}
[[steps]]
skill="crucible.unload"
params={to_slot="slot"}
timeout_s=20
""")
    service = OrchestratorService(
        settings={
            "MG400_ORCH_ZONES_FILE": str(config),
            "MG400_ORCH_ZONE_STATE_FILE": str(tmp_path / "occupancy.json"),
        },
        recipes_dir=catalog,
        runs_dir=tmp_path / "runs",
        demo=True,
    )
    recipe, plan = service.prepare_run({"recipe": "crucible"})
    assert set(plan.required_services) == {"robot", "stage", "furnace", "thermal"}
    assert service.resources is None and service.store is None


class CrucibleRunnerAdapter:
    timeout = 0.05

    def __init__(self, device):
        self.device = device
        self.stops = 0
        self.released = False

    def describe(self):
        extra = self.device.describe() if hasattr(self.device, "describe") else {}
        return {**extra, "contract": 1, "lease_seconds": 3}

    def acquire(self):
        return self.state()

    def heartbeat(self):
        assert self.owned(self.state())

    def state(self):
        return self.device.state()

    def owned(self, state):
        return self.device.owned(state)

    def healthy(self, state):
        return state.get("syncing") is True if state.get("mode") == "robot" else state["connected"]

    def run(self, action, *, timeout, **params):
        return self.device.run(action, **params)

    def command(self, action, **params):
        return self.device.command(action, **params)

    def roi(self, name):
        return self.device.roi(name)

    def stop(self):
        self.stops += 1
        return {"stop": "confirmed"}

    def release(self):
        self.released = True

    def close(self):
        pass


@pytest.mark.parametrize("unsafe", ["furnace", "thermal"])
def test_crucible_interlock_stops_real_runner_before_accepted_robot_call_drains(
    crucible_geometry,
    tmp_path,
    monkeypatch,
    unsafe,
):
    _crucible_files(tmp_path, monkeypatch)
    ctx = CrucibleContext(unloading=unsafe == "thermal")
    entered, finish = threading.Event(), threading.Event()
    original = ctx.clients["robot"].run

    def blocked(action, **params):
        if action == "execute":
            entered.set()
            assert finish.wait(5)
        return original(action, **params)

    ctx.clients["robot"].run = blocked
    registry = default_registry()
    skill, params = (
        ("crucible.load", {"from_slot": "slot"})
        if unsafe == "furnace"
        else ("crucible.unload", {"to_slot": "slot"})
    )
    recipe = validate(
        {
            "name": "guard",
            "version": 1,
            "params": {},
            "steps": [
                {"skill": skill, "params": params, "timeout_s": 10, "on_fail": "hold"},
            ],
        },
        registry,
    )
    services = {name: CrucibleRunnerAdapter(ctx.clients[name]) for name in recipe.services}
    with RunStore(tmp_path / "guard-runs") as store:
        runner = Runner(recipe, registry, services, store).start()
        try:
            assert entered.wait(3), runner.snapshot()
            if unsafe == "furnace":
                ctx.clients["furnace"].value["pv_c"] = 100
            else:
                ctx.clients["thermal"].reading["max_c"] = 100
            until(lambda: runner.status == "held", timeout=2)
            assert not finish.is_set()
            assert all(service.stops >= 1 and not service.released for service in services.values())
            assert not runner.snapshot()["settled"]
            finish.set()
            runner.abort()
            until(lambda: runner.status == "aborted", timeout=4)
            assert runner.wait_closed(4)
            assert all(service.stops >= 2 and service.released for service in services.values())
            assert len(ctx.clients["robot"].calls) == 1
        finally:
            finish.set()
            runner.abort()
            assert runner.wait_closed(5)


class FakeThermalOpener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append((request, timeout))
        value = next(self.responses)
        if isinstance(value, BaseException):
            raise value
        return io.BytesIO(value)


def thermal_adapter(monkeypatch, responses):
    adapter = ThermalAdapter(
        "http://127.0.0.1:1/thermal", "s" * 32, "offline-roi-read", timeout=0.2
    )
    opener = FakeThermalOpener(responses)
    monkeypatch.setattr(adapter._http, "_opener", opener)
    return adapter, opener


def test_named_roi_is_a_version_checked_get_without_lease_or_frame_fallback(monkeypatch):
    expected = {"name": "crucible_1-A", "max_c": 42.0}
    adapter, opener = thermal_adapter(
        monkeypatch, [b'{"contract":1}', json.dumps(expected).encode()]
    )
    assert adapter.roi("crucible_1-A") == expected
    assert [request.full_url for request, _ in opener.requests] == [
        "http://127.0.0.1:1/thermal/api/describe",
        "http://127.0.0.1:1/thermal/api/roi?name=crucible_1-A",
    ]
    assert all(
        request.get_method() == "GET" and request.data is None for request, _ in opener.requests
    )
    assert all(
        request.get_header("Authorization") == "Bearer " + "s" * 32
        for request, _ in opener.requests
    )
    assert all(timeout == 0.2 for _, timeout in opener.requests)


@pytest.mark.parametrize(
    "name", [None, True, 12, "", "a" * 65, "../frame", "x&name=y", "two words", "炉"]
)
def test_roi_refuses_nonexplicit_or_invalid_name_before_any_request(monkeypatch, name):
    adapter, opener = thermal_adapter(monkeypatch, [])
    with pytest.raises(ValueError, match="Region names"):
        adapter.roi(name)
    assert opener.requests == []


def test_roi_rejects_wrong_contract_before_reading_region(monkeypatch):
    adapter, opener = thermal_adapter(monkeypatch, [b'{"contract":2}'])
    with pytest.raises(ServiceError, match="Unsupported service contract"):
        adapter.roi("crucible")
    assert len(opener.requests) == 1


@pytest.mark.parametrize("payload", [b"not-json", b"[]", b"null", b"42", b'"text"'])
def test_roi_refuses_malformed_or_nonobject_json_without_retry(monkeypatch, payload):
    adapter, opener = thermal_adapter(monkeypatch, [b'{"contract":1}', payload])
    with pytest.raises(ServiceError) as error:
        adapter.roi("crucible")
    assert error.value.status == 502
    assert len(opener.requests) == 2


def test_thermal_selection_retains_direct_no_redirect_transport(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://ambient.invalid:9")
    adapters = from_settings(
        {"MG400_ORCH_THERMAL_URL": "http://127.0.0.1:1/thermal"}, identity="offline-selection"
    )
    adapter = adapters["thermal"]
    assert isinstance(adapter, ThermalAdapter)
    handlers = adapter._http._opener.handlers
    assert not any(getattr(handler, "proxies", {}) for handler in handlers)
    redirect = next(handler for handler in handlers if hasattr(handler, "redirect_request"))
    assert redirect.redirect_request(None, None, 302, "", {}, "http://other.invalid") is None
    opener = FakeThermalOpener(
        [
            b'{"contract":1}',
            urllib.error.HTTPError(
                "http://127.0.0.1:1/thermal/api/roi", 302, "Found", {}, io.BytesIO(b"")
            ),
        ]
    )
    monkeypatch.setattr(adapter._http, "_opener", opener)
    with pytest.raises(ServiceError) as error:
        adapter.roi("crucible")
    assert error.value.status == 302
    assert len(opener.requests) == 2


@pytest.mark.parametrize("method,args", [("describe", ()), ("roi", ("crucible",))])
def test_step_read_is_copied_and_refused_after_revocation(method, args):
    shared = {"nested": {"values": [1]}}
    calls = []

    def read(*params):
        calls.append(params)
        return shared

    service = SimpleNamespace(**{method: read})
    authority = _Authority(time.monotonic() + 5)
    client = StepClient(authority, service)
    result = getattr(client, method)(*args)
    result["nested"]["values"].append(2)
    assert shared == {"nested": {"values": [1]}}
    authority.revoke()
    with pytest.raises(Revoked):
        getattr(client, method)(*args)
    assert calls == [args]
    assert authority.drained


@pytest.mark.parametrize("method,args", [("describe", ()), ("roi", ("crucible",))])
def test_inflight_read_is_drained_and_result_refused_when_authority_revoked(method, args):
    entered, finish = threading.Event(), threading.Event()
    errors = []

    def read(*params):
        entered.set()
        assert finish.wait(3)
        return {"name": "crucible"}

    authority = _Authority(time.monotonic() + 5)
    client = StepClient(authority, SimpleNamespace(**{method: read}))

    def invoke():
        try:
            getattr(client, method)(*args)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=invoke)
    worker.start()
    try:
        assert entered.wait(2)
        assert not authority.drained
        authority.revoke()
        finish.set()
        worker.join(2)
        assert not worker.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], Revoked)
        assert authority.drained
    finally:
        finish.set()
        worker.join(3)


def test_context_reads_are_revocable_copies_of_prepared_state(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(runner_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    authority = _Authority(15.0)
    definitions = {"furnace-tube": {"owner": "stage", "conditions": ["robot_clear"]}}
    zones = SimpleNamespace(
        plan=SimpleNamespace(definitions=definitions), bind=lambda *args: object()
    )
    context = Context(authority, {}, object(), "run", 0, None, 0.01, zones)
    definitions["furnace-tube"]["conditions"].append("new-after-prepare")
    first = context.zone_definitions()
    assert first == {"furnace-tube": {"owner": "stage", "conditions": ["robot_clear"]}}
    first["furnace-tube"]["conditions"].clear()
    assert context.zone_definitions()["furnace-tube"]["conditions"] == ["robot_clear"]
    assert context.remaining_s() == 5
    clock[0] = 13.5
    assert context.remaining_s() == 1.5
    authority.revoke()
    for read in (context.remaining_s, context.zone_definitions):
        with pytest.raises(Revoked):
            read()


@pytest.mark.parametrize("failure", ["expired", "lease-loss"])
def test_context_read_refuses_expired_or_lost_authority(monkeypatch, failure):
    monkeypatch.setattr(runner_module, "time", SimpleNamespace(monotonic=lambda: 10.0))
    authority = _Authority(
        10.0 if failure == "expired" else 20.0, failure=lambda: failure == "lease-loss"
    )
    zones = SimpleNamespace(plan=SimpleNamespace(definitions={}), bind=lambda *args: object())
    context = Context(authority, {}, object(), "run", 0, None, 1, zones)
    for read in (context.remaining_s, context.zone_definitions):
        with pytest.raises(Revoked):
            read()


def guarded_context():
    authority = _Authority(time.monotonic() + 5)
    zones = SimpleNamespace(plan=SimpleNamespace(definitions={}), bind=lambda *args: object())
    return Context(authority, {}, object(), "run", 0, None, 1, zones), authority


def test_guard_checks_entry_monitor_and_successful_exit_then_unregisters():
    context, _ = guarded_context()
    calls = []
    with context.guard(lambda: calls.append("check")):
        assert calls == ["check"]
        context.check_guards()
        assert calls == ["check", "check"]
    assert calls == ["check", "check", "check"]
    context.check_guards()
    assert len(calls) == 3


@pytest.mark.parametrize("phase", ["entry", "monitor", "exit"])
def test_guard_failure_revokes_even_if_skill_catches_it(phase):
    context, authority = guarded_context()
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == (1 if phase == "entry" else 2):
            raise RuntimeError("unsafe condition")

    with pytest.raises(RuntimeError, match="unsafe condition"):
        with context.guard(check):
            if phase == "monitor":
                context.check_guards()
    with pytest.raises(Revoked):
        context.check()
    assert authority.drained


def test_exceptional_guard_body_keeps_its_original_error():
    context, _ = guarded_context()
    calls = []
    failure = ValueError("original body failure")
    with pytest.raises(ValueError) as caught:
        with context.guard(lambda: calls.append("check")):
            raise failure
    assert caught.value is failure
    assert calls == ["check"]
    with pytest.raises(Revoked):
        context.check_guards()
    assert calls == ["check"]


def test_revoked_context_never_invokes_a_saved_or_new_callback():
    context, authority = guarded_context()
    calls = []
    with pytest.raises(Revoked):
        with context.guard(lambda: calls.append("check")):
            authority.revoke()
            with pytest.raises(Revoked):
                context.check_guards()
    with pytest.raises(Revoked):
        with context.guard(lambda: calls.append("new")):
            pytest.fail("revoked guard entered")
    assert calls == ["check"]


@pytest.mark.parametrize("body_error", [False, True])
def test_guard_exit_drains_old_predicate_before_next_phase(body_error):
    context, authority = guarded_context()
    entered, release, leave, phase_done, body_entered = (threading.Event() for _ in range(5))
    events, errors, monitor_errors = [], [], []
    failure = ValueError("body failed")

    def check():
        if threading.current_thread().name == "offline-monitor":
            entered.set()
            assert release.wait(3)
            events.append("old-read-finished")

    def skill():
        try:
            with context.guard(check):
                body_entered.set()
                assert leave.wait(3)
                if body_error:
                    raise failure
            events.append("next-phase")
        except BaseException as error:
            errors.append(error)
        finally:
            phase_done.set()

    def monitor_checks():
        try:
            context.check_guards()
        except BaseException as error:
            monitor_errors.append(error)

    worker = threading.Thread(target=skill)
    monitor = threading.Thread(target=monitor_checks, name="offline-monitor")
    worker.start()
    try:
        assert body_entered.wait(2)
        monitor.start()
        assert entered.wait(2)
        leave.set()
        assert not phase_done.wait(0.05)
        assert not authority.drained
        release.set()
        monitor.join(2)
        worker.join(2)
        assert not monitor.is_alive() and not worker.is_alive()
        assert authority.drained
        assert events == (
            ["old-read-finished"] if body_error else ["old-read-finished", "next-phase"]
        )
        assert errors == ([failure] if body_error else [])
        if body_error:
            assert len(monitor_errors) == 1 and isinstance(monitor_errors[0], Revoked)
            with pytest.raises(Revoked):
                context.check_guards()
        else:
            assert monitor_errors == []
            context.check_guards()
        assert events.count("old-read-finished") == 1
    finally:
        release.set()
        leave.set()
        worker.join(3)
        if monitor.ident is not None:
            monitor.join(3)


def test_runner_guard_failure_stops_while_robot_run_is_still_blocked(tmp_path):
    registry, service = Registry(), FakeService()
    moving, finish, unsafe = (threading.Event() for _ in range(3))
    contexts = []

    def blocked_run(action, *, timeout, **params):
        moving.set()
        assert finish.wait(4)
        service.events.append("accepted-write-drained")
        return {"ok": True}

    service.run = blocked_run

    def first(ctx):
        contexts.append(ctx)

        def condition():
            if unsafe.is_set():
                raise RuntimeError("furnace interlock changed")

        with ctx.guard(condition):
            ctx.clients["robot"].run("move")
        service.events.append("next-phase")

    registry.register("first", first, services=("robot",))
    with running(tmp_path, registry, ["first"], {"robot": service}, timeout=3, gates=(finish,)) as (
        runner,
        _,
    ):
        assert moving.wait(2)
        unsafe.set()
        until(lambda: runner.status == "held" and "stop" in service.events)
        assert "accepted-write-drained" not in service.events
        assert "release" not in service.events
        with pytest.raises(Revoked):
            contexts[0].clients["robot"].command("late-motion")
        finish.set()
        until(lambda: runner.snapshot()["settled"])
        assert "next-phase" not in service.events
        assert "late-motion" not in service.events
        assert service.events[-1] == "stop"
        assert service.events.index("accepted-write-drained") < len(service.events) - 1


def test_removed_guard_is_skipped_by_an_older_monitor_snapshot():
    context, _ = guarded_context()
    entered, release = threading.Event(), threading.Event()
    second_calls, errors = [], []

    def first():
        if threading.current_thread().name == "offline-monitor":
            entered.set()
            assert release.wait(3)

    def monitor_checks():
        try:
            context.check_guards()
        except BaseException as error:
            errors.append(error)

    monitor = threading.Thread(target=monitor_checks, name="offline-monitor")
    with context.guard(first):
        second = context.guard(lambda: second_calls.append("check"))
        second.__enter__()
        monitor.start()
        try:
            assert entered.wait(2)
            # This also proves callback I/O does not retain the registry lock.
            second.__exit__(None, None, None)
            assert second_calls == ["check", "check"]
            release.set()
            monitor.join(2)
            assert not monitor.is_alive()
            assert second_calls == ["check", "check"]
            assert errors == []
        finally:
            release.set()
            monitor.join(3)


@pytest.mark.parametrize("failure_phase", ["body", "final-check"])
def test_guard_failure_stops_before_blocked_monitor_read_drains(tmp_path, failure_phase):
    registry, service = Registry(), FakeService()
    entered, release, final_check = (threading.Event() for _ in range(3))

    def skill(ctx):
        def condition():
            if threading.current_thread().name == "orch-zones":
                entered.set()
                assert release.wait(4)
            elif final_check.is_set():
                raise RuntimeError("synchronous final guard failed")

        with ctx.guard(condition):
            assert entered.wait(2)
            if failure_phase == "body":
                raise ValueError("guarded body failed")
            final_check.set()
        service.events.append("next-phase")

    registry.register("first", skill, services=("robot",))
    with running(
        tmp_path, registry, ["first"], {"robot": service}, timeout=3, gates=(release,)
    ) as (runner, _):
        assert entered.wait(2)
        until(lambda: runner.status == "held" and "stop" in service.events)
        snapshot = runner.snapshot()
        assert not release.is_set()
        assert not snapshot["settled"]
        assert "failed" in snapshot["detail"]
        assert "release" not in service.events
        runner.abort()
        assert not runner.wait_closed(0.02)
        assert "release" not in service.events
        release.set()
        assert runner.wait_closed(3)
        assert runner.status == "aborted"
        assert service.events.count("stop") >= 2
        assert service.events[-2:] == ["release", "close"]
        assert "next-phase" not in service.events


def test_monitor_error_survives_worker_revocation_before_outcome_publication(tmp_path, monkeypatch):
    registry, service = Registry(), FakeService()
    guard_entered, monitor_revoked, worker_unwound, release_monitor = (
        threading.Event() for _ in range(4)
    )
    original_error = RuntimeError("robot_clear: original feedback sample is stale")
    recorded = {}
    revoke = _Authority.revoke
    check_held = runner_module.Zones.check_held

    def observed_revoke(authority, error=None):
        revoke(authority, error)
        if threading.current_thread().name == "orch-zones":
            recorded["error"] = error
            recorded["authority"] = authority
            monitor_revoked.set()
            # Model preemption after immediate revocation, before the monitor
            # publishes its outcome. The skill can unwind in this interval.
            assert release_monitor.wait(4)

    def failing_monitor(zones, authority):
        if threading.current_thread().name == "orch-zones":
            assert guard_entered.wait(2)
            raise original_error
        return check_held(zones, authority)

    monkeypatch.setattr(_Authority, "revoke", observed_revoke)
    monkeypatch.setattr(runner_module.Zones, "check_held", failing_monitor)

    def skill(ctx):
        try:
            with ctx.guard(lambda: None):
                guard_entered.set()
                ctx.sleep(3)
        finally:
            worker_unwound.set()
        service.events.append("next-phase")

    registry.register("first", skill, services=("robot",))
    with running(
        tmp_path, registry, ["first"], {"robot": service}, timeout=3, gates=(release_monitor,)
    ) as (runner, _):
        assert monitor_revoked.wait(2)
        assert worker_unwound.wait(2)
        until(lambda: runner.status == "held" and "stop" in service.events)
        snapshot = runner.snapshot()
        assert snapshot["detail"] == str(original_error)
        assert recorded["error"] is original_error
        assert recorded["authority"].revocation_error is original_error
        assert recorded["authority"].cancelled.is_set()
        assert runner._monitor.is_alive()
        assert not snapshot["settled"]
        assert service.events.count("acquire") == 1
        assert "release" not in service.events
        assert "next-phase" not in service.events
        runner.abort()
        assert not runner.wait_closed(0.02)
        assert "release" not in service.events
        release_monitor.set()
        assert runner.wait_closed(3)
        assert runner.status == "aborted"
        assert service.events.count("stop") >= 2
        assert service.events[-2:] == ["release", "close"]
        assert "next-phase" not in service.events


@pytest.mark.parametrize("source", ["clients", "zones"])
def test_guard_unwind_preserves_first_service_or_zone_failure(tmp_path, monkeypatch, source):
    registry, service = Registry(), FakeService()
    coordinator_joining, runner_ready, release_monitor = (threading.Event() for _ in range(3))
    observed = {}
    original_thread = threading.Thread
    original_check_held = runner_module.Zones.check_held
    reason = f"{source}: original ownership or interlock failure"

    class CoordinatedThread(original_thread):
        def join(self, timeout=None):
            if self.name == "orch-skill":
                # Release the skill only after the coordinator has checked the
                # healthy state and committed to waiting for this worker.
                coordinator_joining.set()
                return super().join(2)
            return super().join(timeout)

    def blocked_monitor(zones, authority):
        if threading.current_thread().name == "orch-zones":
            assert release_monitor.wait(4)
        return original_check_held(zones, authority)

    monkeypatch.setattr(runner_module.threading, "Thread", CoordinatedThread)
    monkeypatch.setattr(runner_module.Zones, "check_held", blocked_monitor)

    def skill(ctx):
        with ctx.guard(lambda: None):
            assert coordinator_joining.wait(2)
            assert runner_ready.wait(2)
            runner = observed["runner"]
            target = runner.clients if source == "clients" else runner.zones
            target.error = reason
            ctx.check()
        service.events.append("next-phase")

    registry.register("first", skill, services=("robot",))
    with running(
        tmp_path, registry, ["first"], {"robot": service}, timeout=3, gates=(release_monitor,)
    ) as (runner, _):
        observed["runner"] = runner
        runner_ready.set()
        until(lambda: runner.status == "held" and "stop" in service.events)
        snapshot = runner.snapshot()
        stored = runner._authority.revocation_error
        assert isinstance(stored, Revoked)
        assert snapshot["detail"] == reason
        assert str(stored) == snapshot["detail"]
        assert runner._authority.cancelled.is_set()
        assert not runner._worker.is_alive()
        assert runner._monitor.is_alive()
        assert not snapshot["settled"]
        assert service.events.count("acquire") == 1
        assert "release" not in service.events
        assert "next-phase" not in service.events
        # A later diagnostic cannot overwrite the first recorded cause.
        runner._authority.revoke(RuntimeError("later cleanup failure"))
        assert runner._authority.revocation_error is stored
        with pytest.raises(Revoked) as cancelled:
            runner._authority.check()
        assert str(cancelled.value) == str(stored)
        runner.abort()
        assert not runner.wait_closed(0.02)
        assert "release" not in service.events
        release_monitor.set()
        assert runner.wait_closed(3)
        assert runner.status == "aborted"
        assert service.events.count("stop") >= 2
        assert service.events[-2:] == ["release", "close"]
        assert "next-phase" not in service.events


@pytest.mark.parametrize("unloading", [False, True])
@pytest.mark.parametrize("speed", [True, 0, 1.5, 21])
def test_crucible_speed_refusal_precedes_stage_and_robot_commands(
    crucible_geometry, unloading, speed
):
    ctx = CrucibleContext(unloading=unloading)
    with pytest.raises(ValueError, match="speed|cap"):
        (unload if unloading else load)(ctx, "slot", speed=speed)
    assert all(not client.calls for name, client in ctx.clients.items() if name != "thermal")


@pytest.mark.parametrize("unloading", [False, True])
def test_crucible_routes_use_endpoint_heights_and_selected_speed(crucible_geometry, unloading):
    from test_orchestrator_transfer import _station_flange, tips

    ctx = CrucibleContext(unloading=unloading)
    source, target = ("crucible-load", "slot") if unloading else ("slot", "crucible-load")
    stations = crucible_geometry.load()
    ctx.clients["robot"].value["actual"][:4] = _station_flange(stations[source])
    (unload if unloading else load)(ctx, "slot", speed=20)
    first, last = stations[source], stations[target]

    def pose(station, raised=False):
        return Pose(
            station["x_mm"],
            station["y_mm"],
            station["z_mm"] + (station["approach_mm"] if raised else 0),
            station["r_deg"],
        )

    assert tips(ctx.clients["robot"]) == [
        pose(first, True),
        pose(first),
        pose(first, True),
        pose(last, True),
        pose(last),
        pose(last, True),
    ]
    assert all(
        params["speed"] == 20
        for action, params in ctx.clients["robot"].calls
        if action == "execute"
    )


def test_initial_station_zone_preflight_precedes_any_crucible_command(crucible_geometry):
    crucible_geometry.save("start", Pose(203, 4, 45, 0), approach_mm=10, zone="outside")
    ctx = CrucibleContext()
    with pytest.raises(ValueError, match="robot"):
        load(ctx, "slot")
    assert not ctx.clients["stage"].calls and not ctx.clients["robot"].calls
