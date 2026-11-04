"""Guarded station transfers. No automatic initialization, recovery or motion retries."""

import math
import os
import sys
from dataclasses import dataclass

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
    "MG400_ORCH_TRAY_FILE",
    "MG400_ORCH_TRAY_CAMERA",
    "MG400_ORCH_TRAY_GRIP",
    "MG400_ORCH_TRAY_RELEASE",
    "MG400_ORCH_CHIP_AXIS",
    "MG400_ORCH_CHIP_R_OFFSET_DEG",
    "MG400_ORCH_CHIP_R_MIN_DEG",
    "MG400_ORCH_CHIP_R_MAX_DEG",
}


def _position(value, label):
    if type(value) is not int or not 0 <= value <= 1000:
        raise ValueError(f"{label} must be an integer from 0 to 1000.")
    return value


class _RouteZones:
    """Report successful departure or exceptional exit to the zone contexts.

    The zone manager retains occupied or unknown zones on exceptional exit.
    Normal exits follow the measured departure or taught approach clearance.
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


@dataclass(frozen=True)
class _Route:
    start: dict
    destination: dict
    via: tuple


class _Transfer:
    def __init__(self, ctx, speed=1):
        self.ctx, self.robot = ctx, ctx.clients["robot"]
        if type(speed) is not int or not 1 <= speed <= 100:
            raise ValueError("Transfer speed must be an integer from 1 to the robot service cap.")
        self.speed = speed
        self._speed_checked = False
        self.workspace = Workspace.from_settings(os.environ)
        self.stations = StationStore(
            self.workspace, os.environ.get("MG400_ORCH_STATIONS_FILE") or DEFAULT_STATIONS
        ).load()
        self.offset = load_tool(os.environ.get("MG400_ORCH_TOOL_FILE") or DEFAULT_TOOL)
        self.owner = self.instance = None
        self._station_names = {id(station): name for name, station in self.stations.items()}
        self._measured_source = None
        self._planned = False

    def check_speed(self):
        # Read-only users of state() do not need motion capabilities. Planning
        # checks before any command; move() also protects direct calibration moves.
        if self._speed_checked:
            return
        self.ctx.check()
        try:
            spec = self.robot.describe()["commands"]["execute"]["params"]["properties"]["speed"]
            cap = spec["maximum"]
            valid = (
                spec["type"] == "integer" and type(spec["minimum"]) is int and spec["minimum"] == 1
            )
        except (KeyError, TypeError):
            raise ValueError("Robot service must advertise its execute speed cap.") from None
        if not valid or type(cap) is not int or not 1 <= cap <= 100 or self.speed > cap:
            raise ValueError("Transfer speed must not exceed a valid robot service cap.")
        self._speed_checked = True

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

    def measured_source(self, name, *, x_mm, y_mm, r_deg):
        """Copy one validated observed source, retaining its taught physical constraints.

        The tray skill supplies calibrated robot XY and an explicitly resolved
        flange R. This method does not infer a chip angle, target, height or route.
        Configure the copy once, before complete route preflight. Stored stations
        and the ordinary route lookup remain unchanged.
        """
        if self._planned or self._measured_source is not None:
            raise ValueError("Configure one measured source before planning the transfer.")
        route = self.route(name)
        taught = route[-1]
        observed = {
            **taught,
            "x_mm": finite(x_mm, "measured source x_mm"),
            "y_mm": finite(y_mm, "measured source y_mm"),
            "r_deg": finite(r_deg, "measured source r_deg"),
            "via": list(taught["via"]),
        }
        for approach in (False, True):
            fingertip_to_flange(self._inside(self.pose(observed, approach=approach)), self.offset)
        self._station_names[id(observed)] = name
        self._measured_source = observed
        try:
            # A measured point may move along a groove, but cannot acquire a
            # second logical station identity at contact or departure height.
            for approach in (False, True):
                flange = fingertip_to_flange(self.pose(observed, approach=approach), self.offset)
                self.starting_station(
                    {"actual": [flange.x_mm, flange.y_mm, flange.z_mm, flange.r_deg]},
                    preferred=observed,
                )
        except BaseException:
            del self._station_names[id(observed)]
            self._measured_source = None
            raise
        return [*route[:-1], observed]

    def same_station(self, first, second):
        return self._station_names[id(first)] == self._station_names[id(second)]

    def move(self, pose, expected=None):
        self.check_speed()
        self.state(expected)
        target = fingertip_to_flange(self._inside(pose), self.offset)
        self.robot.run(
            "execute",
            targets={"x": target.x_mm, "y": target.y_mm, "z": target.z_mm, "r": target.r_deg},
            speed=self.speed,
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

    def matches(self, state, pose):
        target = fingertip_to_flange(self._inside(pose), self.offset)
        return all(
            math.isclose(value, requested, rel_tol=0, abs_tol=0.1)
            for value, requested in zip(
                state["actual"][:4], (target.x_mm, target.y_mm, target.z_mm, target.r_deg)
            )
        )

    @staticmethod
    def pose(station, *, approach=False):
        return Pose(
            station["x_mm"],
            station["y_mm"],
            station["z_mm"] + (station["approach_mm"] if approach else 0),
            station["r_deg"],
        )

    def starting_station(self, state, *, preferred=None):
        matches = []
        stations = list(self.stations.values())
        if self._measured_source is not None:
            stations.append(self._measured_source)
        for station in stations:
            self.ctx.check()
            if self.matches(state, self.pose(station)) or self.matches(
                state, self.pose(station, approach=True)
            ):
                matches.append(station)
        if len({self._station_names[id(station)] for station in matches}) != 1:
            raise ValueError(
                "Position the robot at one unambiguous taught station or its approach "
                "before starting a transfer."
            )
        # Original and observed poses can share one logical station. A planned
        # departure must still match its exact physical variant after a wait.
        if preferred is not None and any(station is preferred for station in matches):
            return preferred
        return matches[0]

    def plan(self, *routes):
        self._planned = True
        self.check_speed()
        # Check complete routes, including conversions that a later leg needs,
        # before any arm, gripper or crucible stage command is admitted.
        for route in routes:
            for station in route:
                self.ctx.check()
                fingertip_to_flange(self._inside(self.pose(station)), self.offset)
            endpoint = route[-1]
            fingertip_to_flange(self._inside(self.pose(endpoint, approach=True)), self.offset)
        state, _ = self.state()
        start = self.starting_station(state)
        plans = []
        for route in routes:
            destination = route[-1]
            via = list(route[:-1]) if not self.same_station(destination, start) else []
            # A leading start waypoint is already visited. Keep every other
            # taught via in its declared order, including repeated later visits.
            if via and self.same_station(via[0], start):
                via.pop(0)
            for station in (start, destination):
                fingertip_to_flange(self._inside(self.pose(station, approach=True)), self.offset)
            departure = fingertip_to_flange(self.pose(start, approach=True), self.offset)
            if (
                self.starting_station(
                    {"actual": [departure.x_mm, departure.y_mm, departure.z_mm, departure.r_deg]},
                    preferred=start,
                )
                is not start
            ):
                raise ValueError("The planned departure approach must identify its taught station.")
            plans.append(_Route(start, destination, tuple(via)))
            start = destination
        return tuple(plans)

    def travel(self, route, zones, expected=None):
        state, _ = self.state(expected)
        if self.starting_station(state, preferred=route.start) is not route.start:
            raise RuntimeError("Robot left the planned starting station before travel.")
        zones.enter(route.start["zone"])
        # Zone admission may wait. Its earlier measured pose cannot authorize
        # a rise after the robot has moved away from the planned station.
        state, _ = self.state(expected)
        if self.starting_station(state, preferred=route.start) is not route.start:
            raise RuntimeError("Robot left the planned starting station during zone admission.")
        approach = self.pose(route.start, approach=True)
        if not self.matches(state, approach):
            self.move(approach, expected)
        zones.arrived(route.start["zone"])
        for station in route.via:
            zones.enter(station["zone"])
            self.move(self.pose(station), expected)
            zones.arrived(station["zone"])
        if route.destination is not route.start:
            zones.enter(route.destination["zone"])
            self.move(self.pose(route.destination, approach=True), expected)
            zones.arrived(route.destination["zone"])

    def pick(self, route, grip, opening, zones):
        state, _ = self.state()
        if state["gripper"]["outcome"] == "contact":
            raise RuntimeError("Pick refuses an existing grasp.")
        self.travel(route, zones)
        self.grip(opening, "arrived")
        self.move(self.pose(route.destination), "arrived")
        self.grip(grip, "contact", "arrived")
        self.move(self.pose(route.destination, approach=True), "contact")

    def place(self, route, release, zones):
        state, _ = self.state("contact")
        if release <= state["actual"][4]:
            raise ValueError("Release must open beyond the measured grasp position.")
        self.travel(route, zones, "contact")
        self.move(self.pose(route.destination), "contact")
        self.grip(release, "arrived", "contact")
        self.move(self.pose(route.destination, approach=True), "arrived")


def _opening(grip):
    raw = os.environ.get("MG400_ORCH_PRE_GRIP", "")
    if not raw.isascii() or not raw.isdecimal():
        raise ValueError("Set MG400_ORCH_PRE_GRIP to an explicit integer opening.")
    opening = _position(int(raw), "pre-grip opening")
    if opening <= grip:
        raise ValueError("Pre-grip opening must be greater than the closing target.")
    return opening


def pick(ctx, station: str, grip: int, *, speed: int = 1):
    grip = _position(grip, "grip")
    opening = _opening(grip)
    operation = _Transfer(ctx, speed)
    route = operation.route(station)
    (planned,) = operation.plan(route)
    with _RouteZones(ctx.zones) as zones:
        operation.pick(planned, grip, opening, zones)


def place(ctx, station: str, release: int, *, speed: int = 1):
    release = _position(release, "release")
    operation = _Transfer(ctx, speed)
    route = operation.route(station)
    (planned,) = operation.plan(route)
    with _RouteZones(ctx.zones) as zones:
        operation.place(planned, release, zones)


def transfer(ctx, from_station: str, to_station: str, grip: int, release: int, *, speed: int = 1):
    grip, release = _position(grip, "grip"), _position(release, "release")
    if release <= grip:
        raise ValueError("Release must be greater than the closing target.")
    opening = _opening(grip)
    operation = _Transfer(ctx, speed)
    source, destination = operation.route(from_station), operation.route(to_station)
    source, destination = operation.plan(source, destination)
    with _RouteZones(ctx.zones) as zones:
        operation.pick(source, grip, opening, zones)
        operation.place(destination, release, zones)
