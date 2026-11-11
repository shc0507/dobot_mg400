"""Console workspace: Motion (robot arm and stage).

The page sends every device command through the console relay. This module
adds these routes under ``<base>/api/console/ws/motion``:

- ``GET state`` reads the robot and stage state once per request with a fresh
  read identity and keeps only the fields the page shows. It never acquires,
  renews or releases a lease.
- ``POST jog`` and ``POST move`` send one robot ``execute`` target once through
  ``service.console.command``. The robot service applies its speed cap and refuses
  a move while the controller reports an alarm. A jog adds one step on one axis
  to a fresh read of the actual pose.
- ``POST keys`` ``{"seq", "held"}`` forwards one held-key set of keyboard jog
  once on the robot session of the page that holds robot control. The page arms
  and disarms the keyboard with the described ``keyboard`` command.

``GET stage/reference/<motor>`` checks the camera reference marker of one axis
and returns the marked frame and the pixel offset. Stage ``connect`` and
``teach_reference`` are described commands that the page sends through the relay.

Hold-to-move uses the described ``hold`` command family on this page's stage
session. The console, not the page, sends the stage commands:

- ``POST stage/hold`` ``{"id", "motor", "direction", "speed"}`` starts a hold
  with a new id. The page sends the same body again every 150 ms. Without it
  for 0.5 s the console releases the hold.
- ``POST stage/release`` ``{"id"}`` ends that hold.
"""

import re

from flask import jsonify, request

from labkit.errors import ServiceError
from labkit.lease import validate_client

from . import Workspace

JOG_STEPS = (0.5, 2, 10)
AXES = ("x", "y", "z", "r")

DEVICES = ("robot", "stage")

ROBOT_FIELDS = (
    "actual",
    "joints",
    "enabled",
    "robot_mode",
    "robot_error",
    "alarms",
    "message",
    "busy",
    "locked",
    "stop_unconfirmed",
    "recoverable",
    "syncing",
    "feedback_fresh",
    "vacuum",
    "keyboard_enabled",
    "keyboard_arming",
    "keyboard_speed_percent",
    "keyboard_disarm_reason",
    "demo",
)
STAGE_FIELDS = (
    "connected",
    "demo",
    "error",
    "busy",
    "locked",
    "fault",
    "feedback_fresh",
    "ports",
    "microstep",
    "microstep_modes",
    "limits",
    "defaults",
)
MOTOR_FIELDS = (
    "id",
    "mode",
    "position",
    "position_mm",
    "homed",
    "soft",
    "automation_ready",
    "homing_available",
    "zero_available",
    "named_positions",
    "at_positions",
    "available",
    "cooldown",
)
COMMAND_FIELDS = ("action", "result", "message", "finished_at")


def _pick(source, fields):
    return {key: source[key] for key in fields if key in source}


def _last_command(value):
    return _pick(value, COMMAND_FIELDS) if isinstance(value, dict) else None


def _holder(service, device, value, page):
    """Return mine, console, other or None. Identities stay on the server."""
    if value is None:
        return None
    for item in service.console.control(page):
        if item["device"] == device and item["holder"] == value:
            return "mine" if item["mine"] else "console"
    return "other"


def _robot(state):
    result = _pick(state, ROBOT_FIELDS)
    result["last_command"] = _last_command(state.get("last_command"))
    return result


def _stage(state):
    result = _pick(state, STAGE_FIELDS)
    result["last_command"] = _last_command(state.get("last_command"))
    motors = []
    for motor in state.get("motors") or ():
        if not isinstance(motor, dict):
            continue
        row = _pick(motor, MOTOR_FIELDS)
        reference = motor.get("reference")
        row["reference_configured"] = isinstance(reference, dict)
        motors.append(row)
    result["motors"] = motors
    automation = state.get("automation") if isinstance(state.get("automation"), dict) else {}
    poses = automation.get("poses") if isinstance(automation.get("poses"), list) else []
    result["poses"] = [name for name in poses if isinstance(name, str)][:16]
    at = automation.get("at_poses") if isinstance(automation.get("at_poses"), dict) else {}
    result["at_poses"] = [
        name for name, value in at.items() if isinstance(value, dict) and value.get("matched")
    ]
    return result


READERS = {"robot": _robot, "stage": _stage}


def read(service, device, page, *, fresh=False):
    """Read one device from the device cache. A missing state becomes an unavailable row."""
    entry = service.devices.fresh(device) if fresh else service.devices.wait(device)
    if entry["phase"] in {"unconfigured", "offline"}:
        # Demo: Take control starts the owned demo service on first use.
        return {"available": False, "reason": "offline" if service.demo else entry["phase"]}
    if entry["phase"] != "ready":
        return {"available": False, "reason": "unreachable"}
    try:
        result = READERS[device](entry["state"])
    except Exception:
        return {"available": False, "reason": "unreachable"}
    result["available"] = True
    result["holder"] = _holder(service, device, entry["holder"], page)
    return result


def _targets(value):
    if not isinstance(value, dict) or set(value) != set(AXES):
        raise ValueError("Send targets with x, y, z and r only.")
    for axis in AXES:
        if type(value[axis]) not in {int, float}:
            raise ValueError("Targets must contain finite x, y, z and r.")
    return {axis: float(value[axis]) for axis in AXES}


def _speed(data):
    speed = data.get("speed")
    if type(speed) is not int or speed < 1:
        raise ValueError("Speed must be a whole number from 1 to the service cap.")
    return speed


def _page():
    value = request.headers.get("X-MG400-Client")
    if value is None:
        raise ServiceError(400, "Send the page identity in X-MG400-Client.")
    return validate_client(value)


def _send(service, page, target, speed):
    result = service.console.command("robot", page, "execute", {"targets": target, "speed": speed})
    status = {"accepted": 202, "refused": 409, "unknown": 502}[result["outcome"]]
    return jsonify(result), status


def _body(fields):
    data = request.get_json()
    if not isinstance(data, dict) or set(data) != fields:
        raise ValueError("Send " + ", ".join(sorted(fields)) + " only.")
    return data


def _motor(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", value):
        raise ValueError("Unknown stage axis.")
    return value


def _hold_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", value):
        raise ValueError("The hold id must have 16 to 64 letters, digits, - or _.")
    return value


def register(blueprint, service):
    @blueprint.get("/stage/reference/<motor>")
    def stage_reference(motor):
        view = service.console.read("stage", f"api/motors/{_motor(motor)}/reference")
        image = view.get("image")
        if not isinstance(image, str) or not image.startswith("data:image/jpeg;"):
            image = None
        offset = view.get("offset_px")
        distance = view.get("distance_px")
        return jsonify(
            image=image,
            offset_px=offset if isinstance(offset, list) else None,
            distance_px=distance if type(distance) in {int, float} else None,
        )

    @blueprint.post("/stage/hold")
    def stage_hold():
        page = _page()
        data = _body({"id", "motor", "direction", "speed"})
        direction, speed = data["direction"], data["speed"]
        if direction not in ("cw", "ccw") or type(speed) is not int or speed < 1:
            raise ValueError("Send a direction of cw or ccw and a whole speed of at least 1.")
        hold_id, motor = _hold_id(data["id"]), _motor(data["motor"])
        result = service.console.stage_hold(page, hold_id, motor, direction, speed)
        status = {"accepted": 200, "refused": 409, "unknown": 502}[result["outcome"]]
        return jsonify(result), status

    @blueprint.post("/stage/release")
    def stage_release():
        page = _page()
        data = _body({"id"})
        return jsonify(service.console.stage_release(page, _hold_id(data["id"])))

    @blueprint.get("/state")
    def state():
        page = request.headers.get("X-MG400-Client")
        page = validate_client(page) if page else None
        return jsonify({device: read(service, device, page) for device in DEVICES})

    @blueprint.post("/jog")
    def jog():
        page = _page()
        data = _body({"axis", "direction", "step", "speed"})
        axis, direction, step = data["axis"], data["direction"], data["step"]
        if axis not in AXES or direction not in (1, -1) or type(direction) is not int:
            raise ValueError("Choose an axis (x, y, z or r) and a direction of 1 or -1.")
        if type(step) not in {int, float} or step not in JOG_STEPS:
            raise ValueError("The jog step must be 0.5, 2 or 10.")
        speed = _speed(data)
        # The target is relative to the actual pose, so a cached pose is too old.
        robot = read(service, "robot", page, fresh=True)
        actual = robot.get("actual") if robot.get("available") else None
        pose = actual[:4] if isinstance(actual, list) and len(actual) >= 4 else None
        if pose is None or any(type(value) not in {int, float} for value in pose):
            raise ServiceError(409, "The actual pose is unknown. Wait for the arm to stop.")
        target = dict(zip(AXES, (float(value) for value in pose)))
        target[axis] = round(target[axis] + direction * step, 3)
        return _send(service, page, target, speed)

    @blueprint.post("/keys")
    def keys():
        page = _page()
        data = _body({"seq", "held"})
        if type(data["seq"]) is not int or data["seq"] < 0 or not isinstance(data["held"], list):
            raise ValueError("Send seq as a whole number and held as a list of keys.")
        result = service.console.keys(page, data["seq"], data["held"])
        status = {"accepted": 200, "refused": 409, "unknown": 502}[result["outcome"]]
        return jsonify(result), status

    @blueprint.post("/move")
    def move():
        page = _page()
        data = _body({"targets", "speed"})
        return _send(service, page, _targets(data["targets"]), _speed(data))


WORKSPACE = Workspace(
    id="motion", order=20, title={"en": "Motion", "zh": "运动"}, register=register
)
