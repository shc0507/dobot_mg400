"""Validate taught stations, private files and read-only robot teaching with fakes."""

import copy
import io
import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from orchestrator import cli, stations
from orchestrator.stations import StationStore, Workspace, load_tool, measured_pose, teach, validate
from orchestrator.tool import Pose, ToolOffset


@pytest.fixture
def workspace():
    return Workspace(
        {"x_mm": [0, 500], "y_mm": [-300, 300], "z_mm": [0, 250], "r_deg": [-720, 720]}
    )


@pytest.fixture
def store(workspace, tmp_path):
    return StationStore(workspace, tmp_path / "stations.toml")


@pytest.fixture
def state():
    return {
        "mode": "robot",
        "syncing": True,
        "feedback_fresh": True,
        "controller_id": "panel-holder",
        "busy": False,
        "locked": False,
        "stop_unconfirmed": False,
        "actual": [230, 20, 50, 90, None],
    }


def station(**changes):
    return {
        "x_mm": 230,
        "y_mm": 20,
        "z_mm": 50,
        "r_deg": 90,
        "approach_mm": 20,
        "zone": "tray",
        "saved_at": 123.5,
        **changes,
    }


def settings(workspace):
    return {
        f"MG400_ORCH_{axis}_{side}_{'DEG' if axis == 'R' else 'MM'}": str(
            workspace.bounds[field][index]
        )
        for field, axis in stations.AXES.items()
        for index, side in enumerate(("MIN", "MAX"))
    }


def test_workspace_requires_all_explicit_finite_ordered_settings(workspace):
    values = settings(workspace)
    assert Workspace.from_settings(values) == workspace
    for key in values:
        for invalid in (None, "", "nan", "inf", True):
            candidate = {**values, key: invalid}
            with pytest.raises(ValueError):
                Workspace.from_settings(candidate)
        with pytest.raises(ValueError):
            Workspace.from_settings({k: v for k, v in values.items() if k != key})
    with pytest.raises(ValueError):
        Workspace.from_settings({**values, "MG400_ORCH_X_MIN_MM": "500"})


@pytest.mark.parametrize(
    "changes",
    [
        {"x_mm": -1},
        {"y_mm": 301},
        {"z_mm": 251},
        {"r_deg": 721},
        {"approach_mm": 9.99},
        {"approach_mm": True},
        {"approach_mm": float("inf")},
        {"z_mm": 245, "approach_mm": 10},
        {"saved_at": -1},
        {"saved_at": float("nan")},
        {"zone": None},
        {"zone": "bad zone"},
        {"via": "other"},
        {"via": [None]},
        {"via": ["absent"]},
        {"extra": 1},
        {"x_mm": True},
        {"x_mm": 10**1000},
    ],
)
def test_station_validation_rejects_unsafe_values(workspace, changes):
    with pytest.raises(ValueError):
        validate({"place": station(**changes)}, workspace)


def test_full_via_graph_rejects_cycles_duplicates_and_preserves_valid_order(workspace):
    graph = {
        "start": station(via=["middle", "end"]),
        "middle": station(via=["end"]),
        "end": station(),
    }
    assert validate(graph, workspace)["start"]["via"] == ["middle", "end"]
    for via in (["start"], ["end", "end"]):
        candidate = copy.deepcopy(graph)
        candidate["end"]["via"] = via
        with pytest.raises(ValueError):
            validate(candidate, workspace)


def test_store_round_trip_private_atomic_file_and_referenced_deletion(store):
    pose = Pose(230, 20, 50, 90)
    assert store.load() == {}
    store.save("via", pose, approach_mm=10)
    saved = store.save("target", pose, approach_mm=20, zone="tray", via=["via"])
    assert saved["saved_at"] > 0
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o640
    assert store.load()["target"] == saved
    original = store.path.read_bytes()
    with pytest.raises(ValueError, match="existing"):
        store.remove("via")
    assert store.path.read_bytes() == original
    store.save("target", Pose(240, 20, 50, 90), approach_mm=20, zone="tray")
    assert store.load()["target"]["via"] == ["via"]
    store.remove("target")
    store.remove("via")
    assert store.load() == {}


def test_invalid_update_leaves_original_unchanged(store):
    store.save("a", Pose(230, 20, 50, 90), approach_mm=20)
    original = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.save("a", Pose(230, 20, 50, 90), approach_mm=20, via=["a"])
    assert store.path.read_bytes() == original


def test_failed_atomic_replace_preserves_previous_file_and_cleans_temporary(store, monkeypatch):
    store.save("a", Pose(230, 20, 50, 90), approach_mm=20)
    original = store.path.read_bytes()
    monkeypatch.setattr(stations.os, "replace", Mock(side_effect=OSError("injected failure")))
    with pytest.raises(OSError):
        store.save("b", Pose(230, 20, 50, 90), approach_mm=20)
    assert store.path.read_bytes() == original
    assert sorted(path.name for path in store.path.parent.iterdir()) == ["stations.toml"]


@pytest.mark.parametrize("failure", ["write", "file-fsync", "directory-fsync"])
def test_failed_write_or_sync_reports_the_correct_publication_boundary(store, monkeypatch, failure):
    store.save("a", Pose(230, 20, 50, 90), approach_mm=20)
    original = store.path.read_bytes()
    fdopen, fsync = stations.os.fdopen, stations.os.fsync

    class BrokenWriter:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def fileno(self):
            return self.stream.fileno()

        def write(self, value):
            raise OSError("write failed")

    def open_stream(fd, mode):
        stream = fdopen(fd, mode)
        return BrokenWriter(stream) if failure == "write" and mode == "w" else stream

    def sync(fd):
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if failure == ("directory-fsync" if directory else "file-fsync"):
            raise OSError("sync failed")
        return fsync(fd)

    monkeypatch.setattr(stations.os, "fdopen", open_stream)
    monkeypatch.setattr(stations.os, "fsync", sync)
    with pytest.raises(OSError):
        store.save("b", Pose(230, 20, 50, 90), approach_mm=20)
    if failure == "directory-fsync":
        assert set(store.load()) == {"a", "b"}
    else:
        assert store.path.read_bytes() == original
    assert sorted(path.name for path in store.path.parent.iterdir()) == ["stations.toml"]


def test_parallel_station_updates_do_not_lose_each_other(store):
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(store.save, f"s{i}", Pose(200 + i, 0, 20, 0), approach_mm=10)
            for i in range(20)
        ]
        for future in futures:
            future.result(timeout=5)
    assert set(store.load()) == {f"s{i}" for i in range(20)}


def test_writer_blocks_second_update_until_atomic_publication(store, monkeypatch):
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    replace = stations.os.replace

    def gated(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(3)
        return replace(*args, **kwargs)

    monkeypatch.setattr(stations.os, "replace", gated)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(store.save, "a", Pose(230, 20, 50, 90), approach_mm=20)
        assert entered.wait(3)

        def second():
            second_started.set()
            return store.save("b", Pose(230, 20, 50, 90), approach_mm=20)

        other = executor.submit(second)
        try:
            assert second_started.wait(3)
            assert not other.done()
        finally:
            release.set()
        first.result(timeout=3)
        other.result(timeout=3)
    assert set(store.load()) == {"a", "b"}


@pytest.mark.parametrize(
    "kind", ["file-link", "directory-link", "fifo", "public-file", "public-directory"]
)
def test_store_refuses_symlinks_special_files_and_public_modes(store, tmp_path, kind, monkeypatch):
    if kind == "file-link":
        other = tmp_path / "other"
        other.write_text("[stations]\n")
        other.chmod(0o640)
        store.path.symlink_to(other)
    elif kind == "directory-link":
        link = tmp_path / "link"
        link.symlink_to(tmp_path, target_is_directory=True)
        store.path = link / "stations.toml"
    elif kind == "fifo":
        os.mkfifo(store.path, 0o600)
    elif kind == "public-file":
        store.path.write_text("[stations]\n")
        store.path.chmod(0o644)
    else:
        tmp_path.chmod(0o777)
    original_open = stations.os.open

    def inspect_only(path, flags, *args, **kwargs):
        assert not str(path).startswith("/proc/self/fd/"), "Unsafe inode must never be read"
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(stations.os, "open", inspect_only)
    with pytest.raises((ValueError, OSError)):
        store.load()


def test_tool_file_requires_private_explicit_measured_configuration(tmp_path):
    path = tmp_path / "tool.toml"
    with pytest.raises(FileNotFoundError):
        load_tool(path)
    path.write_text("offset_mm = [3, 4, 5]\n")
    path.chmod(0o640)
    assert load_tool(path) == ToolOffset(3, 4, 5)
    path.write_text("offset_mm = [0, 0, true]\n")
    with pytest.raises(ValueError):
        load_tool(path)


@pytest.mark.parametrize(
    "key,value",
    [
        ("syncing", False),
        ("feedback_fresh", False),
        ("controller_id", None),
        ("controller_id", ""),
        ("controller_id", " "),
        ("controller_id", True),
        ("controller_id", 12),
        ("busy", True),
        ("locked", True),
        ("stop_unconfirmed", True),
        ("mode", "stage"),
        ("actual", [None, None, None, None, 0]),
        ("actual", [1, 2, 3, 4]),
        ("actual", [1, 2, float("nan"), 4, None]),
        ("actual", [True, 2, 3, 4, None]),
    ],
)
def test_teaching_rejects_missing_ownership_stale_busy_unknown_or_invalid_pose(state, key, value):
    state[key] = value
    with pytest.raises(ValueError):
        measured_pose(state)


def test_teach_reads_only_describe_and_measured_state_without_actuation(store, state):
    robot = Mock(spec=["describe", "state"])
    robot.describe.return_value = {"service": "robot"}
    robot.state.return_value = state
    saved = teach(store, "tray", robot, ToolOffset(3, 4, 5), approach_mm=20, zone="tray")
    assert (saved["x_mm"], saved["y_mm"], saved["z_mm"], saved["r_deg"]) == pytest.approx(
        (226, 23, 45, 90)
    )
    assert [call[0] for call in robot.mock_calls] == ["describe", "state"]


def test_cli_save_show_list_remove_with_only_fake_robot_gets(
    store, state, workspace, monkeypatch, tmp_path
):
    tool = tmp_path / "tool.toml"
    tool.write_text("offset_mm = [3, 4, 5]\n")
    tool.chmod(0o640)
    config = {
        **settings(workspace),
        "MG400_ORCH_STATIONS_FILE": str(store.path),
        "MG400_ORCH_TOOL_FILE": str(tool),
        "MG400_ORCH_ROBOT_URL": "http://robot.invalid",
    }
    for key, value in config.items():
        monkeypatch.setenv(key, value)
    robot = Mock(spec=["describe", "state"])
    robot.describe.return_value = {"service": "robot"}
    robot.state.return_value = state
    factory = Mock(return_value=robot)
    monkeypatch.setattr(cli, "ServiceAdapter", factory)
    output = io.StringIO()
    assert (
        cli.main(["station", "save", "tray", "--approach", "20", "--zone", "tray"], out=output) == 0
    )
    assert json.loads(output.getvalue())["x_mm"] == 226
    factory.assert_called_once()
    factory.reset_mock()
    for action, name in (("list", []), ("show", ["tray"]), ("rm", ["tray"])):
        assert cli.main(["station", action, *name], out=io.StringIO()) == 0
    factory.assert_not_called()
    assert store.load() == {}


def test_missing_tool_or_workspace_fails_before_constructing_robot(
    workspace, monkeypatch, tmp_path
):
    factory = Mock(side_effect=AssertionError("no robot client before configuration"))
    monkeypatch.setattr(cli, "ServiceAdapter", factory)
    for key in stations.BOUND_SETTINGS:
        monkeypatch.delenv(key, raising=False)
    arguments = [
        "station",
        "save",
        "tray",
        "--approach",
        "20",
        "--tool-file",
        str(tmp_path / "missing"),
    ]
    assert cli.main(arguments) == 2
    for key, value in settings(workspace).items():
        monkeypatch.setenv(key, value)
    assert cli.main(arguments) == 2
    factory.assert_not_called()
