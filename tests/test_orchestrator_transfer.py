"""Transfer sequencing with fake states and owned SIM-01 loopback services only."""

import copy
import io
import ipaddress
import json
import os
import socket
import time
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from orchestrator import cli
from orchestrator.recipe import validate
from orchestrator.rehearsal import Rehearsal
from orchestrator.runner import Revoked, StepClient, _Authority
from orchestrator.skills import default_registry
from orchestrator.skills.robot import TRANSFER_SETTINGS, pick, place, transfer
from orchestrator.stations import AXES, StationStore, Workspace
from orchestrator.store import RunStore
from orchestrator.tool import Pose, ToolOffset, fingertip_to_flange, flange_to_fingertip


@pytest.fixture(autouse=True)
def owned_loopback_only(monkeypatch):
    connect = socket.socket.connect

    def guarded(sock, address):
        assert ipaddress.ip_address(address[0]).is_loopback
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture
def geometry(tmp_path, monkeypatch):
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
    store = StationStore(workspace, tmp_path / "stations.toml")
    store.save("start", Pose(203, 4, 45, 0), approach_mm=10)
    store.save(
        "demo-start",
        flange_to_fingertip(Pose(250, 10, 40, 30), ToolOffset(3, 4, 5)),
        approach_mm=10,
    )
    store.save("via", Pose(210, 10, 40, 0), approach_mm=30, zone="aisle")
    store.save("source", Pose(230, 20, 30, 90), approach_mm=20, zone="source", via=["via"])
    store.save("target", Pose(260, -20, 20, -90), approach_mm=20, zone="target")
    tool = tmp_path / "tool.toml"
    tool.write_text("offset_mm = [3, 4, 5]\n")
    tool.chmod(0o640)
    monkeypatch.setenv("MG400_ORCH_STATIONS_FILE", str(store.path))
    monkeypatch.setenv("MG400_ORCH_TOOL_FILE", str(tool))
    monkeypatch.setenv("MG400_ORCH_PRE_GRIP", "900")
    return store


class Robot:
    def __init__(self):
        self.value = {
            "mode": "robot",
            "syncing": True,
            "feedback_fresh": True,
            "enabled": True,
            "robot_mode": 5,
            "robot_error": False,
            "busy": False,
            "locked": False,
            "stop_unconfirmed": False,
            "gripper_available": True,
            "gripper_busy": False,
            "keyboard_enabled": False,
            "controller_id": "owned-run",
            "instance": "robot-instance",
            "actual": [200, 0, 50, 0, 1000],
            "gripper": {"outcome": None, "outcome_at": None},
        }
        self.calls = []
        self.after = None
        self.empty = False
        self.missing_reads = 0
        self.reads = 0

    def describe(self):
        return {
            "commands": {
                "execute": {
                    "params": {
                        "properties": {
                            "speed": {"type": "integer", "minimum": 1, "maximum": 20, "default": 1}
                        }
                    }
                }
            }
        }

    def state(self):
        self.reads += 1
        result = copy.deepcopy(self.value)
        if self.missing_reads:
            self.missing_reads -= 1
            result["actual"][:4] = [None] * 4
        return result

    def owned(self, state):
        return state.get("controller_id") == "owned-run"

    def run(self, action, **params):
        self.calls.append((action, params))
        if action == "execute":
            self.value["actual"][:4] = [params["targets"][key] for key in "xyzr"]
        elif action == "grip":
            position = params["position"]
            contact = position == 400 and not self.empty
            self.value["actual"][4] = 600 if contact else position
            self.value["gripper"] = {
                "outcome": "contact" if contact else "arrived",
                "outcome_at": len(self.calls),
            }
        else:
            pytest.fail(f"Unexpected mutation: {action}")
        if self.after:
            self.after(action)


class Context:
    def __init__(self):
        self.robot = Robot()
        self.clients = {"robot": self.robot}
        self.zones = self
        self.events = []
        self.held = set()
        self.checks = 0
        self.cancel_after = None

    def check(self):
        self.checks += 1
        if self.cancel_after is not None and self.checks >= self.cancel_after:
            raise TimeoutError("step deadline")

    def sleep(self, seconds):
        self.check()

    @contextmanager
    def take(self, name):
        self.events.append(("enter", name, set(self.held), self.tip()))
        self.held.add(name)
        try:
            yield
        except BaseException as error:
            self.events.append(("unknown", name, type(error), self.tip()))
            raise
        else:
            self.events.append(("leave", name, set(self.held), self.tip()))
        finally:
            self.held.remove(name)

    def tip(self):
        return flange_to_fingertip(Pose(*self.robot.value["actual"][:4]), ToolOffset(3, 4, 5))


def tips(robot):
    return [
        flange_to_fingertip(Pose(*(params["targets"][key] for key in "xyzr")), ToolOffset(3, 4, 5))
        for action, params in robot.calls
        if action == "execute"
    ]


def test_complete_transfer_uses_taught_heights_vias_offsets_and_explicit_capped_speed(geometry):
    ctx = Context()
    transfer(ctx, "source", "target", 400, 900)
    assert tips(ctx.robot) == [
        Pose(203, 4, 55, 0),
        Pose(210, 10, 40, 0),
        Pose(230, 20, 50, 90),
        Pose(230, 20, 30, 90),
        Pose(230, 20, 50, 90),
        Pose(260, -20, 40, -90),
        Pose(260, -20, 20, -90),
        Pose(260, -20, 40, -90),
    ]
    assert [params["position"] for action, params in ctx.robot.calls if action == "grip"] == [
        900,
        400,
        900,
    ]
    assert all(params["speed"] == 1 for action, params in ctx.robot.calls if action == "execute")
    assert [(event[0], event[1]) for event in ctx.events] == [
        ("enter", "aisle"),
        ("enter", "source"),
        ("leave", "aisle"),
        ("enter", "target"),
        ("leave", "source"),
        ("leave", "target"),
    ]
    assert "source" in ctx.events[3][2]
    assert ctx.events[4][3] == Pose(260, -20, 40, -90)
    assert ctx.events[-1][3] == Pose(260, -20, 40, -90)


@pytest.mark.parametrize("operation", ["pick", "place"])
def test_standalone_zone_exit_requires_measured_approach_arrival(geometry, operation):
    ctx = Context()
    if operation == "pick":
        pick(ctx, "source", 400)
        assert ctx.robot.value["gripper"]["outcome"] == "contact"
    else:
        ctx.robot.value["gripper"] = {"outcome": "contact", "outcome_at": 1}
        ctx.robot.value["actual"][4] = 600
        place(ctx, "source", 900)
        assert ctx.robot.value["gripper"]["outcome"] == "arrived"
    assert ctx.events[-1][0:2] == ("leave", "source")
    assert ctx.events[-1][3] == Pose(230, 20, 50, 90)


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", False),
        ("busy", True),
        ("locked", True),
        ("stop_unconfirmed", True),
        ("feedback_fresh", False),
        ("syncing", False),
        ("robot_error", True),
        ("robot_mode", 7),
        ("gripper_available", False),
        ("gripper_busy", True),
        ("keyboard_enabled", True),
        ("controller_id", ""),
        ("controller_id", True),
        ("instance", None),
        ("instance", ""),
    ],
)
def test_unhealthy_or_uninitialized_state_refuses_before_motion(geometry, field, value):
    ctx = Context()
    ctx.robot.value[field] = value
    with pytest.raises(RuntimeError):
        pick(ctx, "source", 400)
    assert ctx.robot.calls == []


@pytest.mark.parametrize("field", ["controller_id", "instance"])
def test_identity_change_after_motion_refuses_further_commands(geometry, field):
    ctx = Context()
    ctx.robot.after = lambda action: ctx.robot.value.update({field: "replacement"})
    with pytest.raises(RuntimeError):
        pick(ctx, "source", 400)
    assert len(ctx.robot.calls) == 1


def test_first_snapshot_must_belong_to_this_run_before_any_motion(geometry):
    ctx = Context()
    ctx.robot.value["controller_id"] = "another-controller"
    with pytest.raises(RuntimeError):
        transfer(ctx, "source", "target", 400, 900)
    assert ctx.robot.calls == []


@pytest.mark.parametrize("expired", [False, True])
def test_public_ownership_predicate_remains_under_revocable_step_authority(expired):
    service = Mock()
    service.owned.return_value = True
    authority = _Authority(time.monotonic() + (5 if not expired else -1))
    client = StepClient(authority, service)
    state = {"controller_id": "run"}
    if not expired:
        assert client.owned(state) is True
        service.owned.assert_called_once_with(state)
        service.owned.reset_mock()
        authority.revoke()
    with pytest.raises(Revoked):
        client.owned(state)
    service.owned.assert_not_called()


def test_late_destination_conversion_overflow_is_found_before_source_pick(geometry, monkeypatch):
    for axis in ("X", "Y"):
        monkeypatch.setenv(f"MG400_ORCH_{axis}_MIN_MM", "-1.5e308")
        monkeypatch.setenv(f"MG400_ORCH_{axis}_MAX_MM", "1.5e308")
    store = StationStore(Workspace.from_settings(os.environ), geometry.path)
    store.save("target", Pose(1e308, 0, 20, 0), approach_mm=20, zone="target")
    from pathlib import Path

    Path(os.environ["MG400_ORCH_TOOL_FILE"]).write_text("offset_mm = [-1e308, 0, 0]\n")
    ctx = Context()
    with pytest.raises(ValueError, match="finite"):
        transfer(ctx, "source", "target", 400, 900)
    assert ctx.robot.calls == []


@pytest.mark.parametrize("opening", [None, "", "-1", "true", "900.0", "1001", "400"])
def test_pregrip_requires_explicit_larger_integer_without_dispatch(geometry, monkeypatch, opening):
    if opening is None:
        monkeypatch.delenv("MG400_ORCH_PRE_GRIP")
    else:
        monkeypatch.setenv("MG400_ORCH_PRE_GRIP", opening)
    ctx = Context()
    with pytest.raises(ValueError):
        pick(ctx, "source", 400)
    assert ctx.robot.calls == []


@pytest.mark.parametrize(
    "grip,release", [(True, 900), (400.0, 900), (-1, 900), (400, 400), (400, 1001)]
)
def test_invalid_transfer_positions_fail_before_any_motion(geometry, grip, release):
    ctx = Context()
    with pytest.raises(ValueError):
        transfer(ctx, "source", "target", grip, release)
    assert ctx.robot.calls == []


def test_unknown_destination_and_invalid_tool_fail_before_pick(geometry, monkeypatch, tmp_path):
    ctx = Context()
    with pytest.raises(ValueError, match="Unknown station"):
        transfer(ctx, "source", "missing", 400, 900)
    monkeypatch.setenv("MG400_ORCH_TOOL_FILE", str(tmp_path / "missing-tool.toml"))
    with pytest.raises(FileNotFoundError):
        transfer(ctx, "source", "target", 400, 900)
    assert ctx.robot.calls == []


def test_pose_refresh_is_read_only_and_deadline_bounded(geometry):
    ctx = Context()
    ctx.robot.after = lambda action: setattr(ctx.robot, "missing_reads", 2)
    transfer(ctx, "source", "target", 400, 900)
    assert len(ctx.robot.calls) == 11
    ctx = Context()
    ctx.robot.missing_reads = 1000
    ctx.cancel_after = 12
    with pytest.raises(TimeoutError):
        pick(ctx, "source", 400)
    assert ctx.robot.calls == []


@pytest.mark.parametrize("failure", ["empty", "dropped", "wrong-arrival"])
def test_grasp_failure_or_unconfirmed_departure_preserves_exceptional_zone_exit(geometry, failure):
    ctx = Context()
    ctx.robot.empty = failure == "empty"

    def after(action):
        if action == "execute" and ctx.robot.value["gripper"]["outcome"] == "contact":
            if failure == "dropped":
                ctx.robot.value["gripper"].update(outcome="dropped")
            elif failure == "wrong-arrival":
                ctx.robot.value["actual"][2] -= 1

    ctx.robot.after = after
    with pytest.raises(RuntimeError):
        transfer(ctx, "source", "target", 400, 900)
    assert [params["position"] for action, params in ctx.robot.calls if action == "grip"] == [
        900,
        400,
    ]
    assert ctx.events[-1][0:2] == ("unknown", "source")
    assert not any(event[0:2] == ("enter", "target") for event in ctx.events)


def test_release_must_exceed_actual_contact_width_before_departure(geometry):
    ctx = Context()
    with pytest.raises(ValueError, match="measured grasp"):
        transfer(ctx, "source", "target", 400, 500)
    assert ctx.events[-1][0:2] == ("unknown", "source")
    assert not any(event[1] == "target" for event in ctx.events)


def test_public_transfer_aliases_keep_complete_typed_validation():
    registry = default_registry()
    params = {"from": "source", "to": "target", "grip": 400, "release": 900}
    skill = registry["robot.transfer"]
    canonical = skill.validate(params)
    assert canonical == {
        "from_station": "source",
        "to_station": "target",
        "grip": 400,
        "release": 900,
        "speed": 1,
    }
    assert skill.validate(canonical) == canonical
    for invalid in (
        {**params, "extra": 1},
        {**params, "from_station": "source"},
        {**params, "grip": True},
        {**params, "release": 900.0},
        {**params, "from": 1},
        {key: value for key, value in params.items() if key != "to"},
    ):
        with pytest.raises(ValueError):
            validate(
                {
                    "name": "Invalid transfer",
                    "version": 1,
                    "params": {},
                    "steps": [{"skill": "robot.transfer", "params": invalid, "timeout_s": 5}],
                },
                registry,
            )


@pytest.mark.parametrize("outcome", ["contact", "empty", "dropped"])
def test_public_cli_transfer_rehearses_with_owned_collector_and_no_production_settings(
    geometry, tmp_path, monkeypatch, outcome
):
    zone_config = tmp_path / "zones.toml"
    zone_config.write_text(
        "\n".join(
            f'[zones.{name}]\nowner = "robot"\nconditions = []\n'
            for name in ("aisle", "source", "target")
        )
    )
    zone_config.chmod(0o640)
    monkeypatch.setenv("MG400_ORCH_ZONES_FILE", str(zone_config))
    forbidden = {
        "MG400_ORCH_ROBOT_URL",
        "MG400_ORCH_ROBOT_TOKEN",
        "MG400_ROBOT_HOST",
        "MG400_GRIPPER_FEEDBACK",
        "MG400_ORCH_ZONE_STATE_FILE",
        "HTTP_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "PYTHONPATH",
    }
    for key in forbidden:
        monkeypatch.setenv(key, "production-must-not-be-used")
    monkeypatch.delenv("MG400_ORCH_TRAY_FILE", raising=False)
    monkeypatch.setenv("MG400_ORCH_TRAY_CAMERA", "2")
    explicit = {key: os.environ[key] for key in TRANSFER_SETTINGS if key in os.environ}
    absent = TRANSFER_SETTINGS - explicit.keys()
    demos = []

    class Demo(Rehearsal):
        def __enter__(self):
            assert forbidden.isdisjoint(os.environ)
            assert os.environ["MG400_ORCH_ZONES_FILE"] == str(zone_config)
            assert absent.isdisjoint(os.environ)
            assert {
                key: os.environ[key] for key in TRANSFER_SETTINGS if key in os.environ
            } == explicit
            super().__enter__()
            demos.append(self)
            collector = self.simulator.collector
            assert collector.feedback_path.is_relative_to(self.directory)

            def answer(request):
                assert request.get("operation", "position") == "position"
                contact = request["position"] == 400 and outcome != "empty"
                return {
                    "init": 1,
                    "state": 2 if contact else 1,
                    "position": 600 if contact else request["position"],
                }

            if outcome != "empty":
                collector.answer = answer

            execute = self.simulator.controller.execute

            def drop_on_rise(port, text):
                result = execute(port, text)
                if (
                    text.startswith("MovL(")
                    and 400 in collector.targets("answered")
                    and outcome == "dropped"
                ):
                    collector.set(state=3)
                    deadline = time.monotonic() + 1
                    while json.loads(collector.feedback_path.read_text())["state"] != 3:
                        assert time.monotonic() < deadline
                        time.sleep(0.005)
                return result

            monkeypatch.setattr(self.simulator.controller, "execute", drop_on_rise)
            return self

    monkeypatch.setattr(cli, "Rehearsal", Demo)
    recipe = tmp_path / "transfer.toml"
    recipe.write_text("""name = "Transfer demo"
version = 1
params = {}
[[steps]]
skill = "robot.enable"
params = {}
timeout_s = 5
[[steps]]
skill = "robot.transfer"
params = { from = "source", to = "target", grip = 400, release = 900 }
timeout_s = 15
[[steps]]
skill = "sample.record"
params = { chip_id = "demo", note = "complete transfer" }
timeout_s = 1
""")
    output = io.StringIO()
    result = cli.main(
        ["rehearse", str(recipe), "--runs-dir", str(tmp_path / "runs")],
        out=output,
        read=Mock(side_effect=EOFError()),
    )
    report = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith("{")][
        -1
    ]
    assert result == (0 if outcome == "contact" else 1), output.getvalue()
    assert report["status"] == ("completed" if outcome == "contact" else "aborted")
    assert report["unconfirmed_services"] == [], report
    with RunStore(tmp_path / "runs", readonly=True) as store:
        row = store.get(report["run_id"])
        assert row["steps"][-1]["status"] == ("completed" if outcome == "contact" else "pending")
    demo = demos[0]
    assert demo.simulator.collector.targets("published") == (
        [900, 400, 900] if outcome == "contact" else [900, 400]
    )
    assert not demo.simulator.collector._thread.is_alive()
    assert demo.simulator.controller.closed.is_set()
    assert not demo.directory.exists()
    assert os.environ["MG400_ORCH_ROBOT_URL"] == "production-must-not-be-used"


def _route_cap(ctx, maximum=20):
    ctx.robot.describe = lambda: {
        "contract": 1,
        "service": "robot",
        "commands": {
            "execute": {
                "params": {
                    "type": "object",
                    "properties": {
                        "speed": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": maximum,
                            "default": 1,
                        }
                    },
                }
            }
        },
    }


def _station_flange(station, *, raised=False, offset=ToolOffset(3, 4, 5)):
    tip = Pose(
        station["x_mm"],
        station["y_mm"],
        station["z_mm"] + (station["approach_mm"] if raised else 0),
        station["r_deg"],
    )
    pose = fingertip_to_flange(tip, offset)
    return [pose.x_mm, pose.y_mm, pose.z_mm, pose.r_deg]


def _executed_poses(ctx):
    return [
        tuple(params["targets"][axis] for axis in "xyzr")
        for action, params in ctx.robot.calls
        if action == "execute"
    ]


def _without_adjacent_duplicates(points):
    # A redundant no-op dispatch is not the assertion under test here. Every
    # distinct commanded waypoint still has to match the expected route.
    result = []
    for point in points:
        if not result or point != result[-1]:
            result.append(point)
    return result


@pytest.fixture
def h08_route_geometry(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    workspace = Workspace(
        {"x_mm": [-150, 400], "y_mm": [-380, 50], "z_mm": [-130, 60], "r_deg": [-180, 180]}
    )
    for field, axis in AXES.items():
        for index, side in enumerate(("MIN", "MAX")):
            monkeypatch.setenv(
                f"MG400_ORCH_{axis}_{side}_{'DEG' if axis == 'R' else 'MM'}",
                str(workspace.bounds[field][index]),
            )
    store = StationStore(workspace, tmp_path / "stations.toml")
    offset = ToolOffset(0, 0, 146)
    flanges = {
        "T": Pose(-10, -270, 80, 0),
        "V3": Pose(145, -192, 165, 0),
        "V2": Pose(258.6, -114.2, 190, 0),
        "V1": Pose(313.8, -107.8, 175, 0),
        "C": Pose(368.9, -101.3, 143.5, 0),
    }
    for name, flange in flanges.items():
        store.save(name, flange_to_fingertip(flange, offset), approach_mm=10, zone=name)
    for name, via in (("T", ["V1", "V2", "V3"]), ("C", ["V3", "V2", "V1"])):
        store.save(
            name,
            flange_to_fingertip(flanges[name], offset),
            approach_mm=10,
            zone=name,
            via=via,
        )
    tool = tmp_path / "tool.toml"
    tool.write_text("offset_mm = [0, 0, 146]\n")
    tool.chmod(0o640)
    monkeypatch.setenv("MG400_ORCH_STATIONS_FILE", str(store.path))
    monkeypatch.setenv("MG400_ORCH_TOOL_FILE", str(tool))
    monkeypatch.setenv("MG400_ORCH_PRE_GRIP", "900")
    return store, flanges


@pytest.mark.parametrize(
    "source,target,vias",
    [("T", "C", ["V3", "V2", "V1"]), ("C", "T", ["V1", "V2", "V3"])],
)
@pytest.mark.parametrize("at_approach", [False, True])
def test_h08_both_directions_keep_each_taught_height(
    h08_route_geometry, source, target, vias, at_approach
):
    _, flanges = h08_route_geometry
    ctx = Context()  # Existing fake context from the destination test file.
    _route_cap(ctx)
    ctx.tip = lambda: flange_to_fingertip(
        Pose(*ctx.robot.value["actual"][:4]), ToolOffset(0, 0, 146)
    )

    def point(name, approach=False):
        pose = flanges[name]
        return (pose.x_mm, pose.y_mm, pose.z_mm + (10 if approach else 0), pose.r_deg)

    ctx.robot.value["actual"][:4] = point(source, at_approach)
    transfer(ctx, source, target, 400, 900, speed=20)
    expected = [] if at_approach else [point(source, True)]
    expected += [point(source), point(source, True)]
    expected += [point(name) for name in vias]
    expected += [point(target, True), point(target), point(target, True)]
    executed = _without_adjacent_duplicates(_executed_poses(ctx))
    # Drop an optional unchanged first pose, but no route waypoint.
    if at_approach and executed and executed[0] == point(source, True):
        executed.pop(0)
    assert executed == expected
    assert all(params["speed"] == 20 for action, params in ctx.robot.calls if action == "execute")
    for x, y, z, _ in executed:
        matches = [pose for pose in flanges.values() if (pose.x_mm, pose.y_mm) == (x, y)]
        assert len(matches) == 1
        assert z <= matches[0].z_mm + 10
    # Source's incoming vias must not run before grasp when already at source.
    first_grip = next(index for index, call in enumerate(ctx.robot.calls) if call[0] == "grip")
    assert all(
        (params["targets"]["x"], params["targets"]["y"]) == point(source)[:2]
        for action, params in ctx.robot.calls[:first_grip]
        if action == "execute"
    )


def test_incoming_source_vias_are_kept_when_start_is_another_station(geometry):
    stations = geometry.load()
    ctx = Context()
    _route_cap(ctx)
    ctx.robot.value["actual"][:4] = _station_flange(stations["target"])
    pick(ctx, "source", 400)
    expected = [
        tuple(_station_flange(stations["target"], raised=True)),
        tuple(_station_flange(stations["via"])),
        tuple(_station_flange(stations["source"], raised=True)),
        tuple(_station_flange(stations["source"])),
        tuple(_station_flange(stations["source"], raised=True)),
    ]
    assert _without_adjacent_duplicates(_executed_poses(ctx)) == expected


@pytest.mark.parametrize("axis", range(4))
def test_unknown_start_refuses_before_any_robot_command(geometry, axis):
    ctx = Context()
    _route_cap(ctx)
    ctx.robot.value["actual"][:4] = _station_flange(geometry.load()["source"])
    ctx.robot.value["actual"][axis] += 0.101
    with pytest.raises((ValueError, RuntimeError), match="(?i)start|station|position"):
        transfer(ctx, "source", "target", 400, 900)
    assert ctx.robot.calls == []


@pytest.mark.parametrize("alias_pose_z", [30, 10])
def test_distinct_station_pose_or_approach_is_an_ambiguous_start(geometry, alias_pose_z):
    # Alias pose equals source pose, or alias approach equals source pose.
    geometry.save("alias", Pose(230, 20, alias_pose_z, 90), approach_mm=20, zone="alias")
    ctx = Context()
    _route_cap(ctx)
    ctx.robot.value["actual"][:4] = _station_flange(geometry.load()["source"])
    with pytest.raises((ValueError, RuntimeError), match="(?i)start|station|position"):
        transfer(ctx, "source", "target", 400, 900)
    assert ctx.robot.calls == []


def test_start_matching_uses_flange_tolerance_with_rotated_tool(geometry):
    ctx = Context()
    _route_cap(ctx)
    ctx.robot.value["actual"][:4] = _station_flange(geometry.load()["source"])
    ctx.robot.value["actual"][0] += 0.099
    ctx.robot.value["actual"][3] -= 0.099
    # Every flange error is below 0.1, but rotated tool tip X exceeds 0.1.
    measured_tip = ctx.tip()
    assert measured_tip.x_mm - geometry.load()["source"]["x_mm"] > 0.1
    pick(ctx, "source", 400)
    assert ctx.robot.value["gripper"]["outcome"] == "contact"


def test_start_matching_does_not_normalize_a_full_r_turn(geometry):
    ctx = Context()
    _route_cap(ctx)
    ctx.robot.value["actual"][:4] = _station_flange(geometry.load()["source"])
    ctx.robot.value["actual"][3] += 360
    with pytest.raises((ValueError, RuntimeError), match="(?i)start|station|position"):
        pick(ctx, "source", 400)
    assert ctx.robot.calls == []


def test_initial_rise_already_holds_start_zone_and_failure_is_unknown(geometry):
    ctx = Context()
    _route_cap(ctx)
    ctx.robot.value["actual"][:4] = _station_flange(geometry.load()["source"])

    def fail_first_execute(action):
        if action == "execute":
            assert "source" in ctx.held
            raise RuntimeError("uncertain initial rise")

    ctx.robot.after = fail_first_execute
    with pytest.raises(RuntimeError, match="uncertain initial rise"):
        transfer(ctx, "source", "target", 400, 900)
    assert [call[0] for call in ctx.robot.calls] == ["execute"]
    assert ctx.events[0][:2] == ("enter", "source")
    assert any(event[:2] == ("unknown", "source") for event in ctx.events)


@pytest.mark.parametrize("speed", [True, 0, -1, 1.5, "1", 21, 101])
@pytest.mark.parametrize("action", ["pick", "place", "transfer"])
def test_transfer_speed_is_validated_before_any_command(geometry, speed, action):
    ctx = Context()
    functions = {
        "pick": lambda: pick(ctx, "source", 400, speed=speed),
        "place": lambda: place(ctx, "target", 900, speed=speed),
        "transfer": lambda: transfer(ctx, "source", "target", 400, 900, speed=speed),
    }
    with pytest.raises(ValueError, match="speed|cap"):
        functions[action]()
    assert not ctx.robot.calls


@pytest.mark.parametrize("cap", [None, True, 0, 101, 20.0, "20"])
def test_invalid_robot_speed_description_is_not_a_fallback(geometry, cap):
    ctx = Context()
    _route_cap(ctx, cap)
    with pytest.raises(ValueError, match="cap"):
        pick(ctx, "source", 400)
    assert not ctx.robot.calls


def test_changed_start_after_preflight_refuses_before_rise(geometry):
    from orchestrator.skills.robot import _RouteZones, _Transfer

    ctx = Context()
    operation = _Transfer(ctx)
    (route,) = operation.plan(operation.route("source"))
    ctx.robot.value["actual"][:4] = _station_flange(geometry.load()["target"])
    with _RouteZones(ctx.zones) as zones:
        with pytest.raises(RuntimeError, match="planned starting station"):
            operation.pick(route, 400, 900, zones)
    assert not ctx.robot.calls and not ctx.events


def test_future_departure_ambiguity_is_found_before_source_pick(geometry):
    geometry.save("alias", Pose(230, 20, 50, 90), approach_mm=20)
    ctx = Context()
    with pytest.raises(ValueError, match="unambiguous"):
        transfer(ctx, "source", "target", 400, 900)
    assert not ctx.robot.calls


def test_start_is_rechecked_after_zone_admission_before_any_motion(geometry):
    ctx = Context()
    stations = geometry.load()
    ctx.robot.value["actual"][:4] = _station_flange(stations["source"])
    take = ctx.take

    @contextmanager
    def delayed_admission(name):
        with take(name):
            if name == "source":
                ctx.robot.value["actual"][:4] = _station_flange(stations["target"])
            yield

    ctx.take = delayed_admission
    with pytest.raises(RuntimeError, match="during zone admission"):
        transfer(ctx, "source", "target", 400, 900)
    assert not ctx.robot.calls
    assert any(event[:2] == ("unknown", "source") for event in ctx.events)


def test_readonly_state_does_not_require_robot_motion_capabilities(geometry):
    from orchestrator.skills.robot import _Transfer

    ctx = Context()
    ctx.robot.describe = Mock(side_effect=AssertionError("Read-only check queried motion schema"))
    state, pose = _Transfer(ctx).state()
    assert state["actual"] == ctx.robot.value["actual"]
    assert pose == ctx.tip()
    ctx.robot.describe.assert_not_called()
    assert not ctx.robot.calls


@pytest.mark.parametrize("action", ["plan", "move"])
def test_motion_capabilities_are_required_at_plan_and_direct_move_boundaries(geometry, action):
    from orchestrator.skills.robot import _Transfer

    ctx = Context()
    ctx.robot.describe = Mock(return_value={})
    robot = _Transfer(ctx)
    with pytest.raises(ValueError, match="advertise.*speed cap"):
        if action == "plan":
            robot.plan(robot.route("source"))
        else:
            robot.move(robot.pose(geometry.load()["source"]))
    ctx.robot.describe.assert_called_once_with()
    assert not ctx.robot.calls
