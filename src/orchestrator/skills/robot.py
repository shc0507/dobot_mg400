"""Guarded station transfers. No automatic initialization, recovery or motion retries."""

import math
import os
import sys

from ..stations import (
    BOUND_SETTINGS,
    DEFAULT_STATIONS,
    DEFAULT_TOOL,
    StationStore,
    Workspace,
    load_tool,
)
from ..tool import Pose, fingertip_to_flange, finite, flange_to_fingertip

TRANSFER_SETTINGS = BOUND_SETTINGS | {
    "MG400_ORCH_STATIONS_FILE",
    "MG400_ORCH_TOOL_FILE",
    "MG400_ORCH_PRE_GRIP",
}


def _position(value, label):
    if type(value) is not int or not 0 <= value <= 1000:
        raise ValueError(f"{label} must be an integer from 0 to 1000.")
    return value


class _RouteZones:
    """Report successful departure or exceptional exit to the zone contexts.

    ORC-04 can retain occupied or unknown zones on exceptional exit. The current
    carded manager is a no-op and provides no cross-device interlock.
    """

    def __init__(self, manager):
        self.manager, self.held = manager, {}

    def __enter__(self):
        return self

    def enter(self, zone):
        if zone and zone not in self.held:
            context = self.manager.take(zone)
            context.__enter__()
            self.held[zone] = context

    def arrived(self, zone):
        # A source zone stays held throughout the move, until its measured
        # destination and safety state are available.
        for name in list(self.held):
            if name != zone:
                context = self.held.pop(name)
                context.__exit__(None, None, None)

    def __exit__(self, *exc):
        error = None
        for context in reversed(list(self.held.values())):
            try:
                context.__exit__(*exc)
            except BaseException as caught:
                error = caught
                exc = sys.exc_info()
        self.held.clear()
        if error is not None:
            raise error


class _Transfer:
    def __init__(self, ctx):
        self.ctx, self.robot = ctx, ctx.clients["robot"]
        self.workspace = Workspace.from_settings(os.environ)
        self.stations = StationStore(
            self.workspace, os.environ.get("MG400_ORCH_STATIONS_FILE") or DEFAULT_STATIONS
        ).load()
        self.offset = load_tool(os.environ.get("MG400_ORCH_TOOL_FILE") or DEFAULT_TOOL)
        self.owner = self.instance = None

    def _inside(self, pose):
        for axis, (low, high) in self.workspace.bounds.items():
            if not low <= getattr(pose, axis) <= high:
                raise ValueError(f"Transfer {axis} is outside the fingertip workspace.")
        return pose

    def state(self, expected=None):
        while True:
            self.ctx.check()
            state = self.robot.state()
            owner = state.get("controller_id") if isinstance(state, dict) else None
            if (
                not isinstance(state, dict)
                or state.get("mode") != "robot"
                or state.get("syncing") is not True
                or state.get("feedback_fresh") is not True
                or state.get("enabled") is not True
                or state.get("robot_mode") != 5
                or state.get("robot_error") is not False
                or state.get("busy") is not False
                or state.get("locked") is not False
                or state.get("stop_unconfirmed") is not False
                or state.get("gripper_available") is not True
                or state.get("gripper_busy") is not False
                or state.get("keyboard_enabled", False) is not False
                or not isinstance(owner, str)
                or not owner.strip()
                or not isinstance(state.get("instance"), str)
                or not state["instance"].strip()
                or self.robot.owned(state) is not True
            ):
                raise RuntimeError(
                    "Transfer requires owned, fresh, enabled, idle robot state "
                    "and an initialized gripper."
                )
            if self.owner is None:
                self.owner, self.instance = owner, state.get("instance")
            elif owner != self.owner or state.get("instance") != self.instance:
                raise RuntimeError("Robot ownership or instance changed during transfer.")
            actual, grasp = state.get("actual"), state.get("gripper")
            if not isinstance(actual, list) or len(actual) != 5 or not isinstance(grasp, dict):
                raise RuntimeError("Transfer requires measured robot and gripper fields.")
            opening = finite(actual[4], "measured gripper opening")
            if not 0 <= opening <= 1000:
                raise RuntimeError("Measured gripper opening is outside its range.")
            outcome = grasp.get("outcome")
            if outcome == "dropped":
                raise RuntimeError("The gripper reports a dropped object.")
            if outcome not in {None, "arrived", "contact"}:
                raise RuntimeError("The gripper reports an unknown outcome.")
            if expected is not None:
                if outcome != expected:
                    raise RuntimeError(f"Expected gripper outcome {expected}, received {outcome}.")
                if finite(grasp.get("outcome_at"), "gripper outcome timestamp") < 0:
                    raise RuntimeError("Invalid gripper outcome timestamp.")
            if all(value is None for value in actual[:4]):
                # The service clears its pose for each job, then reads a new
                # measured pose after completion. Poll reads only, under the
                # step deadline. Never repeat the completed command.
                self.ctx.sleep(0.02)
                continue
            pose = self._inside(flange_to_fingertip(Pose(*actual[:4]), self.offset))
            self.ctx.check()
            return state, pose

    def route(self, name):
        if not isinstance(name, str) or name not in self.stations:
            raise ValueError(f"Unknown station: {name}")
        stack, route = [(name, False)], []
        while stack:
            self.ctx.check()
            current, visited = stack.pop()
            if visited:
                route.append(self.stations[current])
            else:
                stack.append((current, True))
                stack.extend((other, False) for other in reversed(self.stations[current]["via"]))
        return route

    def move(self, pose, expected=None):
        self.state(expected)
        target = fingertip_to_flange(self._inside(pose), self.offset)
        self.robot.run(
            "execute",
            targets={"x": target.x_mm, "y": target.y_mm, "z": target.z_mm, "r": target.r_deg},
            speed=1,
        )
        state, measured = self.state(expected)
        if not all(
            math.isclose(value, requested, rel_tol=0, abs_tol=0.1)
            for value, requested in zip(
                state["actual"][:4], (target.x_mm, target.y_mm, target.z_mm, target.r_deg)
            )
        ):
            raise RuntimeError("Measured robot pose did not reach the requested waypoint.")
        return state, measured

    def grip(self, target, expected, previous=None):
        self.state(previous)
        self.robot.run("grip", position=target)
        return self.state(expected)

    def travel(self, route, height, zones, expected=None):
        _, current = self.state(expected)
        if current.z_mm != height:
            self.move(Pose(current.x_mm, current.y_mm, height, current.r_deg), expected)
        for station in route:
            zones.enter(station["zone"])
            self.move(Pose(station["x_mm"], station["y_mm"], height, station["r_deg"]), expected)
            zones.arrived(station["zone"])

    @staticmethod
    def pose(station, height=None):
        return Pose(
            station["x_mm"],
            station["y_mm"],
            station["z_mm"] if height is None else height,
            station["r_deg"],
        )

    def pick(self, route, grip, opening, height, zones):
        state, _ = self.state()
        if state["gripper"]["outcome"] == "contact":
            raise RuntimeError("Pick refuses an existing grasp.")
        self.travel(route, height, zones)
        self.grip(opening, "arrived")
        self.move(self.pose(route[-1]), "arrived")
        self.grip(grip, "contact", "arrived")
        self.move(self.pose(route[-1], height), "contact")

    def place(self, route, release, height, zones):
        state, _ = self.state("contact")
        if release <= state["actual"][4]:
            raise ValueError("Release must open beyond the measured grasp position.")
        self.travel(route, height, zones, "contact")
        self.move(self.pose(route[-1]), "contact")
        self.grip(release, "arrived", "contact")
        self.move(self.pose(route[-1], height), "arrived")

    def height(self, *routes):
        _, current = self.state()
        height = max(
            current.z_mm,
            *(station["z_mm"] + station["approach_mm"] for route in routes for station in route),
        )
        self._inside(Pose(current.x_mm, current.y_mm, height, current.r_deg))
        # Validate every route pose and conversion before the first motion,
        # including descent and raised endpoints in both coordinate frames.
        waypoints = [Pose(current.x_mm, current.y_mm, height, current.r_deg)]
        for route in routes:
            for station in route:
                waypoints.extend((self.pose(station), self.pose(station, height)))
        for pose in waypoints:
            self.ctx.check()
            fingertip_to_flange(self._inside(pose), self.offset)
        return height


def _opening(grip):
    raw = os.environ.get("MG400_ORCH_PRE_GRIP", "")
    if not raw.isascii() or not raw.isdecimal():
        raise ValueError("Set MG400_ORCH_PRE_GRIP to an explicit integer opening.")
    opening = _position(int(raw), "pre-grip opening")
    if opening <= grip:
        raise ValueError("Pre-grip opening must be greater than the closing target.")
    return opening


def pick(ctx, station: str, grip: int):
    grip = _position(grip, "grip")
    opening = _opening(grip)
    operation = _Transfer(ctx)
    route = operation.route(station)
    height = operation.height(route)
    with _RouteZones(ctx.zones) as zones:
        operation.pick(route, grip, opening, height, zones)


def place(ctx, station: str, release: int):
    release = _position(release, "release")
    operation = _Transfer(ctx)
    route = operation.route(station)
    height = operation.height(route)
    with _RouteZones(ctx.zones) as zones:
        operation.place(route, release, height, zones)


def transfer(ctx, from_station: str, to_station: str, grip: int, release: int):
    grip, release = _position(grip, "grip"), _position(release, "release")
    if release <= grip:
        raise ValueError("Release must be greater than the closing target.")
    opening = _opening(grip)
    operation = _Transfer(ctx)
    source, destination = operation.route(from_station), operation.route(to_station)
    height = operation.height(source, destination)
    with _RouteZones(ctx.zones) as zones:
        operation.pick(source, grip, opening, height, zones)
        operation.place(destination, release, height, zones)
