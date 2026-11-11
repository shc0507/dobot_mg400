"""Console workspace: Twin (a 3D model of the bench that follows the devices).

The page loads the bench model ``console-static/twin.glb`` (built in the twin
authoring folder, see the docs) and polls this route:

- ``GET state`` reads the robot, stage, microscope and furnace once each with a
  fresh read identity. It returns the fields the page shows, the joint values of
  the model and its signals. It never acquires, renews or releases a lease.

Joint values are relative to the model's own state (the first bench video):
robot J1-J4 in degrees, ``stage_bottom``, ``stage_top`` and ``insert`` in mm,
``turret`` in degrees. A value is ``None`` when it is unknown.

The stage reports only pulse counts. ``STAGE`` maps them; camera snapshots of
the stage markers at half-step measured it (2026-11-11):
mm = sign * pulses / pulses_per_mm + offset. The mapping holds while the counts
keep their reference and the microstep mode stays the same. Otherwise the stage
joints are ``None``.
"""

import uuid

from flask import jsonify, request

from labkit.lease import validate_client

from . import Workspace

DEVICES = ("robot", "stage", "microscope", "furnace")
STAGE_MICROSTEP = 2
STAGE = {  # model joint: (motor, sign, pulses per mm, offset mm)
    "stage_bottom": ("bottom", -1, 80.0, 11.01),
    "stage_top": ("top", -1, 80.0, 2.58),
    "insert": ("insert", -1, 80.0, 39.04),
}
# The model shows objective 1 (10x) at work; 1 turret step = 72 deg (direction not checked).
WORKING_OBJECTIVE = 1
TURRET_STEP_DEG = 72.0
HOT_C = 50.0


def _pick(source, fields):
    return {key: source[key] for key in fields if key in source}


def _robot(state):
    row = _pick(state, ("joints", "actual", "enabled", "busy", "robot_error", "feedback_fresh"))
    row["alarms"] = len(state.get("alarms") or ())
    return row


def _stage(state):
    row = _pick(state, ("connected", "busy", "fault", "feedback_fresh", "microstep"))
    row["motors"] = {
        m["id"]: _pick(m, ("position", "mode"))
        for m in state.get("motors") or ()
        if isinstance(m, dict) and isinstance(m.get("id"), str)
    }
    return row


def _microscope(state):
    return _pick(
        state,
        ("connected", "busy", "fault", "objective_id", "focus_position_pulses", "focus_zero_known"),
    )


def _furnace(state):
    row = _pick(
        state, ("connected", "busy", "fault", "pv_c", "target_c", "control_mode", "run_state")
    )
    flow = state.get("flow") if isinstance(state.get("flow"), dict) else {}
    row["valves_open"] = [
        bool(v.get("open")) for v in flow.get("gas_valves") or () if isinstance(v, dict)
    ]
    row["gas_percent"] = [
        g.get("actual_percent") for g in flow.get("gas_channels") or () if isinstance(g, dict)
    ]
    return row


READERS = {"robot": _robot, "stage": _stage, "microscope": _microscope, "furnace": _furnace}


def _holder(service, device, value, page):
    if value is None:
        return None
    for item in service.console.control(page):
        if item["device"] == device and item["holder"] == value:
            return "mine" if item["mine"] else "console"
    return "other"


def read(service, device, page):
    """Read one device once. Any failure becomes an unavailable row, never an error."""
    if not service.dashboard_configured(device):
        return {"available": False, "reason": "unconfigured"}
    adapter = None
    try:
        adapter = service.console_client(device, f"console-twin-{uuid.uuid4().hex}")
        if adapter is None:
            return {"available": False, "reason": "offline"}
        state = adapter.state()
        if not isinstance(state, dict):
            raise ValueError("Invalid state.")
        row = READERS[device](state)
        lease = state.get("lease")
        holder = lease.get("holder") if isinstance(lease, dict) else state.get("controller_id")
    except Exception:
        return {"available": False, "reason": "unreachable"}
    finally:
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                pass
    row["available"] = True
    row["holder"] = _holder(service, device, holder, page)
    return row


def _number(value):
    return value if type(value) in (int, float) else None


def joints(rows):
    """Model joint values from the device rows (None = unknown)."""
    out = {}
    robot = rows.get("robot", {})
    j = robot.get("joints") if robot.get("available") else None
    for k in range(4):
        out[f"J{k + 1}"] = _number(j[k]) if isinstance(j, list) and len(j) == 4 else None
    stage = rows.get("stage", {})
    same_mode = stage.get("available") and stage.get("microstep") == STAGE_MICROSTEP
    for name, (motor, sign, ppm, offset) in STAGE.items():
        pulses = (
            _number(stage.get("motors", {}).get(motor, {}).get("position")) if same_mode else None
        )
        out[name] = round(sign * pulses / ppm + offset, 2) if pulses is not None else None
    scope = rows.get("microscope", {})
    objective = scope.get("objective_id") if scope.get("available") else None
    out["turret"] = (
        (objective - WORKING_OBJECTIVE) * TURRET_STEP_DEG if type(objective) is int else None
    )
    out["focus"] = None  # focus pulses have no mm scale or zero yet
    return out


def signals(rows):
    """Conditions the page shows on the model."""
    robot, stage, furnace = rows.get("robot", {}), rows.get("stage", {}), rows.get("furnace", {})
    pv = _number(furnace.get("pv_c"))
    return {
        "robot_alarm": bool(robot.get("robot_error") or robot.get("alarms")),
        "robot_stale": not (robot.get("available") and robot.get("feedback_fresh")),
        "robot_moving": bool(robot.get("busy")),
        "stage_stale": not (stage.get("available") and stage.get("feedback_fresh")),
        "stage_moving": bool(stage.get("busy"))
        or any(m.get("mode") not in (None, "idle") for m in stage.get("motors", {}).values()),
        "stage_calibrated": bool(
            stage.get("available") and stage.get("microstep") == STAGE_MICROSTEP
        ),
        "furnace_c": pv,
        "furnace_hot": pv is not None and pv >= HOT_C,
        "furnace_running": furnace.get("control_mode") not in (None, "STOPPED"),
        "gas_on": any(furnace.get("valves_open") or ()),
    }


def register(blueprint, service):
    @blueprint.get("/state")
    def state():
        page = request.headers.get("X-MG400-Client")
        page = validate_client(page) if page else None
        rows = {device: read(service, device, page) for device in DEVICES}
        return jsonify(devices=rows, joints=joints(rows), signals=signals(rows))


WORKSPACE = Workspace(
    id="twin", order=50, title={"en": "Twin", "zh": "实时台面"}, register=register
)
