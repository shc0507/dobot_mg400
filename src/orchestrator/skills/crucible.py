"""Checked crucible transfers. No automatic homing, recovery or motion replay."""

import math
import os
import time
from datetime import datetime
from types import SimpleNamespace

from .robot import _opening, _position, _RouteZones, _Transfer

PREFIX = "MG400_ORCH_CRUCIBLE_"
CRUCIBLE_SETTINGS = {
    PREFIX + suffix
    for suffix in (
        "LOAD_MAX_C",
        "UNLOAD_MAX_C",
        "GRIP",
        "RELEASE",
        "AWAKE_S",
        "MAX_AGE_S",
        "ROIS_FILE",
    )
}


def _number(value, label):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{label} must be a finite number.")
    return float(value)


def _setting(value, suffix, *, integer=False):
    if type(value) in (int, float) and value == -1:
        key = PREFIX + suffix
        raw = os.environ.get(key, "")
        try:
            if not raw.strip() or (integer and not (raw.isascii() and raw.isdecimal())):
                raise ValueError
            value = int(raw) if integer else float(raw)
        except (ValueError, OverflowError):
            raise ValueError(f"Set an explicit finite value for {key}.") from None
    if integer:
        return _position(value, suffix)
    return _number(value, suffix)


def _region_bounds(value):
    names = {"x0", "y0", "x1", "y1"}
    if (
        not isinstance(value, dict)
        or set(value) != names
        or any(type(value[key]) is not int for key in names)
    ):
        raise ValueError("The crucible region requires exact integer pixel bounds.")
    if not (0 <= value["x0"] < value["x1"] <= 160 and 0 <= value["y0"] < value["y1"] <= 120):
        raise ValueError("The crucible region must fit the native 160 by 120 sensor.")
    return dict(value)


class _CheckedRobot:
    def __init__(self, robot, check):
        self.robot, self.check = robot, check

    def state(self):
        return self.robot.state()

    def owned(self, state):
        return self.robot.owned(state)

    def run(self, action, **params):
        self.check()
        return self.robot.run(action, **params)


class _Crucible:
    def __init__(self, ctx, slot, *, unloading, grip, release, max_c, awake_s, max_age_s):
        self.ctx, self.unloading = ctx, unloading
        if not isinstance(slot, str) or slot == "crucible-load":
            raise ValueError("Choose a tray station distinct from crucible-load.")
        self.grip = _setting(grip, "GRIP", integer=True)
        self.release = _setting(release, "RELEASE", integer=True)
        if self.release <= self.grip:
            raise ValueError("Crucible release must exceed grip.")
        self.opening = _opening(self.grip)
        self.max_c = _setting(max_c, "UNLOAD_MAX_C" if unloading else "LOAD_MAX_C")
        self.awake_s = _setting(awake_s, "AWAKE_S")
        self.max_age_s = _setting(max_age_s, "MAX_AGE_S")
        if self.max_c < 0 or not 0 < self.awake_s <= 60 or self.max_age_s <= 0:
            raise ValueError("Use a nonnegative temperature, 0 < awake_s <= 60 and max_age_s > 0.")
        self.awake_budget = math.floor(self.awake_s * 1000) / 1000
        if self.awake_budget <= 0 or ctx.remaining_s() > self.awake_budget:
            raise ValueError(
                "The remaining step budget exceeds the explicit Insert awake interval."
            )
        self.identities = {}
        self.stage_fixed = None
        self.awakened = False
        self.stage, self.furnace = ctx.clients["stage"], ctx.clients["furnace"]
        clients = dict(ctx.clients)
        clients["robot"] = _CheckedRobot(clients["robot"], self.robot_check)
        context = SimpleNamespace(
            clients=clients, check=ctx.check, sleep=ctx.sleep, zones=ctx.zones
        )
        self.robot = _Transfer(context)
        source, target = ("crucible-load", slot) if unloading else (slot, "crucible-load")
        self.source, self.target = self.robot.route(source), self.robot.route(target)
        if self.robot.stations["crucible-load"]["zone"] != "crucible-load":
            raise ValueError("Station crucible-load must use zone crucible-load.")
        definitions = ctx.zone_definitions()
        required = {
            "crucible-load": ("robot", "stage_at_load"),
            "furnace-tube": ("stage", "robot_clear"),
        }
        for route in (self.source, self.target):
            for station in route:
                if station["zone"]:
                    if definitions.get(station["zone"], {}).get("owner") != "robot":
                        raise ValueError("Every transfer station zone must belong to the robot.")
                    required.setdefault(station["zone"], ("robot", None))
        for name, (owner, condition) in required.items():
            spec = definitions.get(name, {})
            if spec.get("owner") != owner or (
                condition is not None and condition not in spec.get("conditions", ())
            ):
                raise ValueError(f"Configure the required owner and entry conditions for {name}.")
        self.height = self.robot.height(self.source, self.target)
        robot_state, _ = self.robot.state()
        if robot_state["gripper"]["outcome"] == "contact":
            raise RuntimeError("A crucible transfer must start without an existing grasp.")
        self.furnace_state()
        state, insert = self.stage_state()
        proof = state["automation"].get("at_poses", {}).get("furnace", {})
        axes = proof.get("axes", {}) if isinstance(proof, dict) else {}
        if any(axes.get(axis) is not True for axis in ("bottom", "top")):
            raise RuntimeError("Bottom and Top must match the calibrated furnace pose.")
        if unloading:
            if proof.get("matched") is not True or axes.get("insert") is not True:
                raise RuntimeError("Unload requires the freshly measured complete furnace pose.")
            self.thermal = ctx.clients["thermal"]
            description = self.thermal.describe()
            self.roi_age = min(
                self.max_age_s, _number(description.get("max_frame_age_s"), "Thermal age limit")
            )
            if self.roi_age <= 0 or "crucible" not in description.get("regions", {}):
                raise RuntimeError(
                    "Configure a named crucible thermal region and a positive age limit."
                )
            self.region_bounds = _region_bounds(description["regions"]["crucible"])
            self.thermal_state()
        elif "load" not in insert.get("at_positions", ()):
            raise RuntimeError("Load requires Insert at its calibrated load position.")

    def generic(self, name):
        self.ctx.check()
        client = self.ctx.clients[name]
        state = client.state()
        if (
            not isinstance(state, dict)
            or state.get("connected") is not True
            or state.get("locked") is not False
            or "fault" not in state
            or state["fault"] is not None
            or client.owned(state) is not True
        ):
            raise RuntimeError(f"{name} requires fresh owned healthy service state.")
        instance = state.get("instance")
        if "instance" in state and (not isinstance(instance, str) or not instance.strip()):
            raise RuntimeError(f"{name} reported an invalid instance identity.")
        lease = state.get("lease", {})
        owner = lease.get("holder") if isinstance(lease, dict) else None
        if not isinstance(owner, str) or not owner.strip():
            raise RuntimeError(f"{name} has no valid lease holder.")
        identity = owner, "instance" in state, instance
        if name in self.identities and self.identities[name] != identity:
            raise RuntimeError(f"{name} ownership or instance changed.")
        self.identities[name] = identity
        self.ctx.check()
        return state

    def age(self, value, maximum, label):
        age = _number(value, label)
        if not 0 <= age <= maximum:
            raise RuntimeError(f"{label} is stale or invalid.")

    def furnace_state(self):
        state = self.generic("furnace")
        sequence = state.get("sequence")
        if (
            type(state.get("run_state_code")) is not int
            or state["run_state_code"] != 1
            or state.get("run_state") != "STOP/COOLING"
            or state.get("control_mode") != "STOPPED"
            or not isinstance(sequence, dict)
            or sequence.get("running") is not False
            or state.get("error") is not None
            or state.get("persistence_error") is not None
            or not (
                state.get("busy") is False
                or (state.get("busy") is True and sequence.get("phase") == "cooling")
            )
        ):
            raise RuntimeError("The furnace must be stopped or cooling, with no running sequence.")
        sampled = _number(state.get("sample_time"), "Furnace sample time")
        if sampled <= 0:
            raise RuntimeError("The furnace has no measured temperature sample.")
        self.age(time.time() - sampled, self.max_age_s, "Furnace sample age")
        pv = _number(state.get("pv_c"), "Furnace PV")
        if not self.unloading and pv >= self.max_c:
            raise RuntimeError("Furnace PV is not below load_max_c.")
        return state

    def stage_state(self, *, moving=False, load=False, awake=False):
        state = self.generic("stage")
        automation = state.get("automation")
        if (
            state.get("feedback_fresh") is not True
            or not isinstance(automation, dict)
            or automation.get("configured") is not True
            or automation.get("error") is not None
            or (
                not moving
                and (state.get("busy") is not False or automation.get("active") is not None)
            )
        ):
            raise RuntimeError("Stage automation requires fresh, configured, idle feedback.")
        self.age(state.get("feedback_age_s"), min(2, self.max_age_s), "Stage feedback age")
        motors = state.get("motors")
        if not isinstance(motors, list) or len(motors) != 3:
            raise RuntimeError("Stage feedback must contain all 3 motors.")
        axes = {motor.get("id"): motor for motor in motors if isinstance(motor, dict)}
        if set(axes) != {"bottom", "top", "insert"} or any(
            motor.get("homed") is not True
            or motor.get("automation_ready") is not True
            or motor.get("soft") is not True
            or motor.get("mode") not in ({"idle", "moving"} if moving else {"idle"})
            for motor in axes.values()
        ):
            raise RuntimeError(
                "Every stage axis requires a current home, soft limits and idle reference."
            )
        fixed = tuple(
            _number(axes[axis].get("position_mm"), f"{axis} position") for axis in ("bottom", "top")
        )
        if self.stage_fixed is None:
            self.stage_fixed = fixed
        elif self.stage_fixed != fixed:
            raise RuntimeError("Bottom or Top moved away from the verified furnace alignment.")
        if any(axes[axis].get("mode") != "idle" for axis in ("bottom", "top")):
            raise RuntimeError("Bottom and Top must remain idle throughout crucible transfer.")
        if not moving:
            proof = automation.get("at_poses", {}).get("furnace", {})
            matches = proof.get("axes", {}) if isinstance(proof, dict) else {}
            if any(matches.get(axis) is not True for axis in ("bottom", "top")):
                raise RuntimeError("Bottom and Top left the calibrated furnace alignment.")
        insert = axes["insert"]
        if load and "load" not in insert.get("at_positions", ()):
            raise RuntimeError("Insert left its calibrated load position.")
        if awake and insert.get("awake") is not True:
            raise RuntimeError("Insert's explicit awake interval expired.")
        return state, insert

    def thermal_state(self):
        state = self.generic("thermal")
        if state.get("busy") is not False or state.get("state") != "streaming":
            raise RuntimeError("Thermal acquisition must be streaming and idle.")
        self.age(state.get("frame_age_s"), self.roi_age, "Thermal frame age")
        if type(state.get("frame_sequence")) is not int or state["frame_sequence"] < 1:
            raise RuntimeError("Thermal acquisition has no valid frame sequence.")
        return state

    def cold(self):
        before = self.thermal_state()
        roi = self.thermal.roi("crucible")
        after = self.thermal_state()
        if not isinstance(roi, dict) or roi.get("name") != "crucible":
            raise RuntimeError("The thermal response must name the crucible region.")
        bounds = _region_bounds({key: roi.get(key) for key in self.region_bounds})
        if bounds != self.region_bounds:
            raise RuntimeError("The thermal response changed the configured crucible region.")
        sequence = roi.get("frame_sequence")
        if (
            type(sequence) is not int
            or not before["frame_sequence"] <= sequence <= after["frame_sequence"]
        ):
            raise RuntimeError("The crucible reading does not belong to a current thermal frame.")
        self.age(roi.get("frame_age_s"), self.roi_age, "Crucible frame age")
        try:
            stamp = datetime.fromisoformat(roi["timestamp"])
            if stamp.utcoffset() is None:
                raise ValueError
            age = time.time() - stamp.timestamp()
        except (KeyError, TypeError, ValueError, OverflowError):
            raise RuntimeError(
                "Crucible reading requires an explicit valid capture timestamp."
            ) from None
        self.age(age, self.roi_age, "Crucible capture age")
        low, mean, high = (_number(roi.get(key), key) for key in ("min_c", "mean_c", "max_c"))
        if not low <= mean <= high:
            raise RuntimeError("Crucible thermal statistics are inconsistent.")
        return high < self.max_c

    def robot_check(self):
        self.furnace_state()
        self.stage_state(load=True, awake=True)
        if self.unloading and not self.cold():
            raise RuntimeError("Crucible temperature is not below unload_max_c.")

    def stage_command(self, action, **params):
        with self.ctx.guard(self.furnace_state):
            return self._stage_command(action, **params)

    def _stage_command(self, action, **params):
        self.furnace_state()
        while True:
            state, insert = self.stage_state(awake=self.awakened)
            source = "furnace" if action == "goto" and params["name"] == "load" else "load"
            if source not in insert.get("at_positions", ()):
                raise RuntimeError(f"Insert left its required starting position: {source}.")
            if source == "furnace" and (
                state["automation"].get("at_poses", {}).get("furnace", {}).get("matched")
                is not True
            ):
                raise RuntimeError("Insert withdrawal requires the complete measured furnace pose.")
            if action != "goto":
                break
            cooldown = _number(insert.get("cooldown"), "Insert cooldown")
            if cooldown < 0:
                raise RuntimeError("Insert reported an invalid cooldown.")
            if insert.get("available") is True:
                break
            # Wait for readiness before the first dispatch. Never repeat a
            # rejected or accepted command to work around the cooldown.
            self.ctx.sleep(0.02)
            self.furnace_state()
        response = self.stage.command(action, **params)
        identity = response.get("command_id") if isinstance(response, dict) else None
        if not isinstance(identity, str) or not identity:
            raise RuntimeError("Stage command acceptance is unknown. Do not repeat it.")
        while True:
            self.furnace_state()
            state, insert = self.stage_state(moving=True, awake=self.awakened)
            command = state.get("last_command")
            if isinstance(command, dict) and command.get("id") == identity:
                if (
                    command.get("action") != action
                    or command.get("client") != self.identities["stage"][0]
                ):
                    raise RuntimeError("Stage completion identity does not match the request.")
                if command.get("result") != "ok":
                    raise RuntimeError("Stage command did not complete successfully.")
                if _number(command.get("finished_at"), "Stage completion time") <= 0:
                    raise RuntimeError("Stage completion has no valid timestamp.")
                state, insert = self.stage_state()
                if action == "goto" and params["name"] not in insert.get("at_positions", ()):
                    raise RuntimeError("Insert did not reach its measured named target.")
                if action == "awake" and insert.get("awake") is not True:
                    raise RuntimeError("Insert did not confirm the requested awake interval.")
                return state
            self.ctx.sleep(0.05)

    def awake(self):
        if self.ctx.remaining_s() > self.awake_budget:
            raise RuntimeError("The remaining step budget exceeds the explicit awake interval.")
        self.stage_command("awake", motor="insert", seconds=self.awake_s)
        self.awakened = True

    def transfer(self):
        with self.ctx.guard(self.robot_check):
            with _RouteZones(self.ctx.zones) as zones:
                self.robot.pick(self.source, self.grip, self.opening, self.height, zones)
                self.robot.place(self.target, self.release, self.height, zones)


def load(
    ctx,
    from_slot: str,
    *,
    grip: int = -1,
    release: int = -1,
    load_max_c: float = -1,
    awake_s: float = -1,
    max_age_s: float = -1,
):
    operation = _Crucible(
        ctx,
        from_slot,
        unloading=False,
        grip=grip,
        release=release,
        max_c=load_max_c,
        awake_s=awake_s,
        max_age_s=max_age_s,
    )
    operation.awake()
    operation.transfer()
    entry = ctx.zones.declare("furnace-tube")
    try:
        operation.stage_command("goto", motor="insert", name="furnace")
    except BaseException:
        ctx.zones.finish(entry, exceptional=True)
        raise
    else:
        # Insert remains physically inside. A successful return is not departure.
        ctx.zones.finish(entry)


def unload(
    ctx,
    to_slot: str,
    *,
    grip: int = -1,
    release: int = -1,
    unload_max_c: float = -1,
    awake_s: float = -1,
    max_age_s: float = -1,
):
    operation = _Crucible(
        ctx,
        to_slot,
        unloading=True,
        grip=grip,
        release=release,
        max_c=unload_max_c,
        awake_s=awake_s,
        max_age_s=max_age_s,
    )
    with ctx.zones.take("furnace-tube"):
        operation.stage_command("goto", motor="insert", name="load")
        operation.stage_state(load=True)
    operation.awake()
    while True:
        operation.furnace_state()
        operation.stage_state(load=True, awake=True)
        if operation.cold():
            break
        ctx.sleep(0.1)
    operation.transfer()
