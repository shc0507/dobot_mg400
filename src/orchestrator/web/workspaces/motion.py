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

The stage operator routes reach stage routes that are not in its description,
so the relay and the runner never send them. Each change needs this page's
stage control and is sent once:

- ``GET stage/ports`` lists the serial ports; ``POST stage/connect``
  ``{"port"}`` connects the Arduino on one listed port.
- ``GET stage/reference/<motor>`` checks the camera reference marker of one
  axis and returns the marked frame and the pixel offset.
- ``POST stage/teach`` ``{"motor", "confirmed": true}`` saves the current marker
  position as the reference of that axis.

Hold-to-move uses the described ``hold`` command family on this page's stage
session. The console, not the page, sends the stage commands:

- ``POST stage/hold`` ``{"id", "motor", "direction", "speed"}`` starts a hold
  with a new id. The page sends the same body again every 150 ms. Without it
  for 0.5 s the console releases the hold.
- ``POST stage/release`` ``{"id"}`` ends that hold.
"""

import re
import uuid

from flask import jsonify, request

from labkit.errors import ServiceError
from labkit.lease import validate_client

from . import Workspace

JOG_STEPS = (0.5, 2, 10)
AXES = ("x", "y", "z", "r")

DEVICES = ("robot", "stage")

ROBOT_FIELDS = (
    "actual",
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
    "gripper_available",
    "gripper_can_initialize",
    "gripper_busy",
    "gripper_message",
    "gripper",
    "keyboard_enabled",
    "keyboard_arming",
    "keyboard_speed_percent",
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
    return result, state.get("controller_id")


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
    lease = state.get("lease")
    return result, lease.get("holder") if isinstance(lease, dict) else None


READERS = {"robot": _robot, "stage": _stage}


def read(service, device, page):
    """Read one device once. Any failure becomes an unavailable row, never an error."""
    if not service.dashboard_configured(device):
        # Demo: Take control starts the owned demo service on first use.
        return {"available": False, "reason": "offline" if service.demo else "unconfigured"}
    adapter = None
    try:
        adapter = service.console_client(device, f"console-motion-{uuid.uuid4().hex}")
        if adapter is None:
            return {"available": False, "reason": "offline"}
        state = adapter.state()
        if not isinstance(state, dict):
            raise ValueError("Invalid state.")
        result, holder = READERS[device](state)
    except Exception:
        return {"available": False, "reason": "unreachable"}
    finally:
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                pass
    result["available"] = True
    result["holder"] = _holder(service, device, holder, page)
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


def _operator(service, page, path, data):
    result = service.console.operator("stage", page, path, data)
    result.pop("result", None)
    status = {"accepted": 200, "refused": 409, "unknown": 502}[result["outcome"]]
    return jsonify(result), status


def register(blueprint, service):
    @blueprint.get("/stage/ports")
    def stage_ports():
        ports = service.console.read("stage", "api/ports").get("ports")
        rows = [
            {"device": item["device"], "description": str(item.get("description", ""))[:120]}
            for item in (ports if isinstance(ports, list) else [])
            if isinstance(item, dict) and isinstance(item.get("device"), str)
        ]
        return jsonify(ports=rows)

    @blueprint.post("/stage/connect")
    def stage_connect():
        page = _page()
        data = _body({"port"})
        if not isinstance(data["port"], str) or not data["port"]:
            raise ValueError("Select a serial port.")
        return _operator(service, page, "api/connect", {"port": data["port"]})

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

    @blueprint.post("/stage/teach")
    def stage_teach():
        page = _page()
        data = _body({"motor", "confirmed"})
        if data["confirmed"] is not True:
            raise ValueError("Confirm the action inside the page.")
        motor = _motor(data["motor"])
        return _operator(service, page, f"api/motors/{motor}/teach_reference", {"confirm": True})

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
        robot = read(service, "robot", page)
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
