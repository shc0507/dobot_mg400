"""Validated fingertip stations and serialized, atomic TOML updates."""

import fcntl
import json
import os
import re
import stat
import time
import tomllib
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from .tool import Pose, ToolOffset, fingertip_to_flange, finite, flange_to_fingertip

DEFAULT_STATIONS = Path("/var/lib/mg400/robot/stations.toml")
DEFAULT_TOOL = Path("/var/lib/mg400/robot/tool.toml")
AXES = {"x_mm": "X", "y_mm": "Y", "z_mm": "Z", "r_deg": "R"}
BOUND_SETTINGS = {
    f"MG400_ORCH_{axis}_{side}_{'DEG' if axis == 'R' else 'MM'}"
    for axis in AXES.values()
    for side in ("MIN", "MAX")
}


def _name(value, label="Station name", *, empty=False):
    if empty and value == "":
        return value
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        raise ValueError(f"{label} must use 1 to 64 letters, digits, underscores or hyphens.")
    return value


@dataclass(frozen=True)
class Workspace:
    bounds: dict

    def __post_init__(self):
        if not isinstance(self.bounds, dict) or set(self.bounds) != set(AXES):
            raise ValueError("Workspace bounds must define x_mm, y_mm, z_mm and r_deg.")
        checked = {}
        for axis, pair in self.bounds.items():
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError(f"{axis} needs minimum and maximum bounds.")
            low, high = (finite(value, axis) for value in pair)
            if low >= high:
                raise ValueError(f"{axis} minimum must be less than its maximum.")
            checked[axis] = (low, high)
        object.__setattr__(self, "bounds", checked)

    @classmethod
    def from_settings(cls, settings):
        bounds = {}
        for field, axis in AXES.items():
            unit = "DEG" if axis == "R" else "MM"
            pair = []
            for side in ("MIN", "MAX"):
                key = f"MG400_ORCH_{axis}_{side}_{unit}"
                try:
                    value = settings[key]
                    if not isinstance(value, str) or not value.strip():
                        raise ValueError
                    pair.append(float(value))
                except (KeyError, ValueError, OverflowError):
                    raise ValueError(f"Set an explicit finite workspace bound: {key}") from None
            bounds[field] = pair
        return cls(bounds)

    def check(self, pose, approach_mm):
        if not isinstance(pose, Pose):
            raise ValueError("Station coordinates require a Pose.")
        approach = finite(approach_mm, "approach_mm")
        if approach < 10:
            raise ValueError("approach_mm must be at least 10.")
        for axis, (low, high) in self.bounds.items():
            if not low <= getattr(pose, axis) <= high:
                raise ValueError(f"Station {axis} is outside the fingertip workspace.")
        raised = finite(pose.z_mm + approach, "approach height")
        if raised > self.bounds["z_mm"][1]:
            raise ValueError("The fingertip approach height is outside the workspace.")


def validate(stations, workspace):
    if not isinstance(stations, dict):
        raise ValueError("stations must be a table.")
    checked = {}
    required = {*AXES, "approach_mm", "zone", "saved_at"}
    for name, values in stations.items():
        _name(name)
        if not isinstance(values, dict) or not required <= set(values) <= required | {"via"}:
            raise ValueError(f"Invalid station fields: {name}")
        pose = Pose(**{axis: values[axis] for axis in AXES})
        workspace.check(pose, values["approach_mm"])
        timestamp = finite(values["saved_at"], "saved_at")
        if timestamp < 0:
            raise ValueError("saved_at must be a nonnegative Unix timestamp.")
        via = values.get("via", [])
        if not isinstance(via, list) or any(not isinstance(item, str) for item in via):
            raise ValueError("via must be a list of station names.")
        if len(set(via)) != len(via) or any(item not in stations for item in via):
            raise ValueError("via names must be unique existing stations.")
        checked[name] = {
            **asdict(pose),
            "approach_mm": finite(values["approach_mm"], "approach_mm"),
            "zone": _name(values["zone"], "Zone", empty=True),
            "saved_at": timestamp,
            "via": list(via),
        }
    # Iterative DFS avoids recursion limits for a user-provided graph.
    complete = set()
    for name in checked:
        active, stack = set(), [(name, False)]
        while stack:
            current, leaving = stack.pop()
            if leaving:
                active.remove(current)
                complete.add(current)
            elif current in active:
                raise ValueError("Station via references must not contain a cycle.")
            elif current not in complete:
                active.add(current)
                stack.append((current, True))
                stack.extend((other, False) for other in reversed(checked[current]["via"]))
    return checked


def measured_pose(state):
    if not isinstance(state, dict):
        raise ValueError("Robot state must be an object.")
    if (
        state.get("mode") != "robot"
        or state.get("syncing") is not True
        or state.get("feedback_fresh") is not True
        or not isinstance(state.get("controller_id"), str)
        or not state["controller_id"].strip()
        or state.get("busy") is not False
        or state.get("locked") is not False
        or state.get("stop_unconfirmed") is not False
    ):
        raise ValueError(
            "Teaching requires fresh, idle, unlocked robot state held by a controller."
        )
    actual = state.get("actual")
    if not isinstance(actual, list) or len(actual) != 4:
        raise ValueError("Robot actual must contain 4 measured coordinates.")
    return Pose(*actual)


def _check(info, *, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode):
        raise ValueError("Station data needs safe file types: a regular file in a directory.")


@contextmanager
def _parent(path):
    path = Path(path).absolute()
    if ".." in path.parts or path.name in {"", ".", ".."}:
        raise ValueError("Configuration paths must not contain parent traversal.")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parent.parts[1:]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd
            )
            os.close(fd)
            fd = child
        _check(os.fstat(fd), directory=True)
        yield fd, path.name
    finally:
        os.close(fd)


def _read_bytes(parent, name):
    # Pin and inspect the inode before opening it for reading: never activate a device.
    fd = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    try:
        _check(os.fstat(fd))
        reader = os.open(f"/proc/self/fd/{fd}", os.O_RDONLY | os.O_CLOEXEC)
        with os.fdopen(reader, "rb") as source:
            return source.read()
    finally:
        os.close(fd)


def _read(parent, name):
    return tomllib.loads(_read_bytes(parent, name).decode("utf-8"))


def _stage(parent, name, data, *, prefix=None):
    """Write and sync a 0640 file; return its name. With prefix=None, create name itself."""
    target = name if prefix is None else f".{prefix}-{uuid.uuid4().hex}"
    fd = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o640,
        dir_fd=parent,
    )
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), 0o640)
            _check(os.fstat(output.fileno()))
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        _discard(parent, target)
        raise
    return target


def _discard(parent, name):
    try:
        os.unlink(name, dir_fd=parent)
    except FileNotFoundError:
        pass


def _render(stations):
    lines = ["[stations]"]
    for station, values in sorted(stations.items()):
        lines.extend(("", f"[stations.{station}]"))
        for key, value in values.items():
            lines.append(f"{key} = {json.dumps(value, ensure_ascii=True, allow_nan=False)}")
    return ("\n".join(lines) + "\n").encode("ascii")


def load_tool(path=DEFAULT_TOOL):
    with _parent(path) as (parent, name):
        return ToolOffset.from_config(_read(parent, name))


class StationStore:
    def __init__(self, workspace, path=DEFAULT_STATIONS):
        self.workspace, self.path = workspace, Path(path)

    def _load(self, parent, name):
        try:
            data = _read(parent, name)
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict) or set(data) != {"stations"}:
            raise ValueError("The station file must contain only a stations table.")
        return validate(data["stations"], self.workspace)

    def load(self):
        with _parent(self.path) as (parent, name):
            fcntl.flock(parent, fcntl.LOCK_SH)
            return self._load(parent, name)

    def _update(self, change):
        with _parent(self.path) as (parent, name):
            # A directory lock is stable across atomic file replacement and
            # serializes every read-modify-write without a removable lock file.
            fcntl.flock(parent, fcntl.LOCK_EX)
            stations = self._load(parent, name)
            change(stations)
            stations = validate(stations, self.workspace)
            temporary = _stage(parent, name, _render(stations), prefix=name)
            try:
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                _discard(parent, temporary)
            return stations

    def retool(self, offset, tool_path=DEFAULT_TOOL):
        """Write a new tool offset and convert the stations so every flange target stays.

        Both old files are first copied to NAME.bak-<UTC time>. Every converted
        station must pass validation before any file changes. The new files are
        staged and synced first; only two renames remain at the end.
        """
        if not isinstance(offset, ToolOffset):
            raise ValueError("A measured ToolOffset is required.")
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        with _parent(tool_path) as (tool_parent, tool_name), _parent(self.path) as (parent, name):
            fcntl.flock(parent, fcntl.LOCK_EX)
            tool_raw = _read_bytes(tool_parent, tool_name)
            old = ToolOffset.from_config(tomllib.loads(tool_raw.decode("utf-8")))
            try:
                stations_raw = _read_bytes(parent, name)
            except FileNotFoundError:
                stations_raw = None
            stations = self._load(parent, name)
            for values in stations.values():
                pose = Pose(**{axis: values[axis] for axis in AXES})
                values.update(asdict(flange_to_fingertip(fingertip_to_flange(pose, old), offset)))
            stations = validate(stations, self.workspace)
            backups = []
            if stations_raw is not None:
                _stage(parent, f"{name}.bak-{stamp}", stations_raw)
                backups.append(f"{Path(self.path).absolute()}.bak-{stamp}")
            _stage(tool_parent, f"{tool_name}.bak-{stamp}", tool_raw)
            backups.append(f"{Path(tool_path).absolute()}.bak-{stamp}")
            text = f"offset_mm = {json.dumps([offset.dx_mm, offset.dy_mm, offset.dz_mm])}\n"
            staged_tool = _stage(tool_parent, tool_name, text.encode("ascii"), prefix=tool_name)
            staged = _stage(parent, name, _render(stations), prefix=name)
            try:
                os.replace(staged, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.replace(staged_tool, tool_name, src_dir_fd=tool_parent, dst_dir_fd=tool_parent)
                os.fsync(parent)
                os.fsync(tool_parent)
            finally:
                _discard(parent, staged)
                _discard(tool_parent, staged_tool)
        return {
            "old_offset_mm": [old.dx_mm, old.dy_mm, old.dz_mm],
            "offset_mm": [offset.dx_mm, offset.dy_mm, offset.dz_mm],
            "backups": backups,
            "stations": stations,
        }

    def save(self, name, pose, *, approach_mm, zone="", via=None):
        _name(name)
        self.workspace.check(pose, approach_mm)
        _name(zone, "Zone", empty=True)

        def change(stations):
            previous = stations.get(name, {})
            stations[name] = {
                **asdict(pose),
                "approach_mm": approach_mm,
                "zone": zone,
                "saved_at": time.time(),
                "via": previous.get("via", []) if via is None else via,
            }

        return self._update(change)[name]

    def remove(self, name):
        _name(name)

        def change(stations):
            if name not in stations:
                raise ValueError(f"Unknown station: {name}")
            del stations[name]

        self._update(change)


def teach(store, name, robot, offset, *, approach_mm, zone=""):
    _name(name)
    approach = finite(approach_mm, "approach_mm")
    if approach < 10:
        raise ValueError("approach_mm must be at least 10.")
    _name(zone, "Zone", empty=True)
    description = robot.describe()
    if description.get("service") != "robot":
        raise ValueError("Station teaching requires the robot service.")
    pose = flange_to_fingertip(measured_pose(robot.state()), offset)
    return store.save(name, pose, approach_mm=approach, zone=zone)
