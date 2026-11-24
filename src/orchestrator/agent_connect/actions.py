"""Reviewed Console actions. This list never derives commands from caller input."""

import copy
import math
import re

from furnace.config import MAX_RAMP, MAX_TEMP
from stage_controller.protocol import (
    MAX_ROTATIONS,
    MAX_SPEED,
    MICROSTEP_MODES,
    MIN_ROTATIONS,
    MIN_SPEED,
    POSITION_LIMIT,
)

REVISION = 3
PHYSICAL_DISABLED = "Physical agent commands are disabled for the 8082 test Console."
FURNACE_DISABLED = "Agent furnace operation is disabled, including recipes and composite actions."


def obj(properties=None, required=None):
    properties = properties or {}
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


def string(**limits):
    return {"type": "string", **limits}


def number(**limits):
    return {"type": "number", **limits}


def array(items, count, minimum=None):
    return {
        "type": "array",
        "items": items,
        "minItems": count if minimum is None else minimum,
        "maxItems": count,
    }


MOTOR = string(enum=["bottom", "top", "insert"])
NAME = string(minLength=1, maxLength=120)
IDENTIFIER = string(minLength=1, maxLength=64)
SPEED = number(minimum=MIN_SPEED, maximum=MAX_SPEED, description="Speed in rotations/s.")
AXES = {axis: number(description="Position in encoder rotations.") for axis in ("x", "y", "insert")}
TEMPERATURE = number(minimum=0, maximum=MAX_TEMP, description="Temperature in degrees C.")
RAMP = number(exclusiveMinimum=0, maximum=MAX_RAMP, description="Ramp rate in degrees C/min.")
GAS = obj({str(channel): number(minimum=0, maximum=100) for channel in range(3)}, [])
SEQUENCE = {
    "steps": {
        "type": "array",
        "minItems": 1,
        "items": obj(
            {
                "temperature_c": TEMPERATURE,
                "ramp_rate_c_per_min": RAMP,
                "hold_minutes": number(minimum=0),
                "gas": GAS,
            },
            ["temperature_c", "ramp_rate_c_per_min"],
        ),
    },
    "end_gas": GAS,
    "end_gas_until_c": TEMPERATURE,
}
RECORDING = {
    "recording_id": string(pattern="^[0-9a-f]{32}$"),
    "run_name": string(maxLength=80),
    "mode": string(enum=["time_lapse", "live"]),
    "capture_interval_seconds": number(minimum=0.1, maximum=3600),
    "output_fps": number(minimum=0.1, maximum=120),
}


def _action(
    identifier,
    label,
    device,
    properties=None,
    defaults=None,
    *,
    route="command",
    command=None,
    required=None,
    reason="",
    description=None,
    revision=REVISION,
):
    return {
        "id": identifier,
        "label": label,
        "description": description or label + ". Uses the reviewed Console handler.",
        "params": obj(properties, required),
        "defaults": defaults or {},
        "device": device,
        "revision": revision,
        "route": route,
        "command": command or identifier.split(".", 1)[1],
        "disabled_reason": reason,
    }


ACTION_SPECS = [
    _action(
        "stage.coordinates",
        "Move stage coordinates",
        "stage",
        {
            "target": {
                "oneOf": [
                    obj({key: AXES[key] for key in ("x", "y")}),
                    obj({"insert": AXES["insert"]}),
                ]
            },
        },
        {"target": {"x": 0, "y": 0}},
        route="transfer",
        command="coordinates",
        revision=4,
    ),
    _action(
        "stage.position",
        "Move one stage axis",
        "stage",
        {
            "motor": MOTOR,
            "position": number(
                minimum=0, maximum=POSITION_LIMIT, description="Position in encoder rotations."
            ),
        },
        {"motor": "insert", "position": 0},
        route="transfer",
        command="position",
    ),
    _action(
        "stage.jog",
        "Jog stage by 0.1 rotation",
        "stage",
        {
            "motor": MOTOR,
            "direction": string(enum=["cw", "ccw"]),
        },
        {"motor": "insert", "direction": "ccw"},
        route="transfer",
        command="jog",
        revision=4,
    ),
    _action(
        "stage.calibrate",
        "Calibrate a stage axis",
        "stage",
        {
            "motor": MOTOR,
            "speed": SPEED,
        },
        {"motor": "insert", "speed": 0.25},
    ),
    _action(
        "stage.step",
        "Move a bounded stage step",
        "stage",
        {
            "motor": MOTOR,
            "direction": string(enum=["cw", "ccw"]),
            "rotations": number(
                minimum=MIN_ROTATIONS,
                maximum=MAX_ROTATIONS,
                description="Distance in encoder rotations.",
            ),
            "speed": SPEED,
        },
        {"motor": "insert", "direction": "ccw", "rotations": 0.1, "speed": 0.25},
        route="transfer",
        command="step",
        revision=4,
    ),
    *[
        _action(
            "stage." + name,
            label,
            "stage",
            {"motor": MOTOR},
            {"motor": "insert"},
            description={
                "reset_position": (
                    "Set the current position to zero and clear both travel limits. No movement."
                ),
                "set_limit": "Record the current nonzero encoder position as its travel bound.",
            }.get(name),
        )
        for name, label in (
            ("clear_fault", "Clear a stage fault"),
            ("reset_position", "Set stage zero"),
            ("set_limit", "Record a stage travel limit"),
            ("clear_upper_limit", "Clear the stage upper limit"),
            ("clear_lower_limit", "Clear the stage lower limit"),
        )
    ],
    _action(
        "stage.connect",
        "Connect the stage",
        "stage",
        {"port": string(minLength=1, maxLength=512)},
    ),
    _action(
        "stage.set_microstep",
        "Set stage microstepping",
        "stage",
        {
            "microstep": {"type": "integer", "enum": list(MICROSTEP_MODES)},
        },
        {"microstep": 8},
    ),
    _action(
        "stage.hold",
        "Hold stage motion",
        "stage",
        reason="Continuous holds require the human Console deadman control.",
    ),
    _action("transfer.extract", "Extract Sample", "stage", route="transfer", command="fully_out"),
    _action("transfer.insert", "Insert Sample", "stage", route="transfer", command="fully_in"),
    _action(
        "transfer.stage",
        "Move to a saved stage target",
        "stage",
        {
            "target": string(enum=["out_stage", "microscope"]),
        },
        {"target": "out_stage"},
        route="transfer",
        command="stage",
        revision=4,
    ),
    _action(
        "transfer.position",
        "Move to a named position",
        "stage",
        {"name": NAME},
        route="transfer",
        command="named",
        revision=4,
    ),
    *[
        _action(
            "transfer." + name.replace("/", "."),
            label,
            "stage",
            fields,
            route="profile",
            command=name,
            required=required,
        )
        for name, label, fields, required in (
            ("profile/new", "Create a transfer draft", {"name": NAME}, []),
            ("record", "Record a named position", {"name": NAME}, None),
            (
                "endpoint/record",
                "Record a stage target",
                {"target": string(enum=["out_stage", "microscope"])},
                None,
            ),
            (
                "endpoint/delete",
                "Delete a draft stage target",
                {"target": string(enum=["out_stage", "microscope"])},
                None,
            ),
            ("position/delete", "Delete a draft position", {"name": NAME}, None),
            ("profile/save", "Save the transfer draft", {"name": NAME}, []),
            ("profile/select", "Select a transfer profile", {"id": IDENTIFIER}, None),
            ("profile/load", "Load a transfer profile", {"id": IDENTIFIER}, None),
            ("profile/delete", "Delete a transfer profile", {"id": IDENTIFIER}, None),
            (
                "profile/upload",
                "Import a transfer profile",
                {"profile": string(minLength=1, maxLength=131072)},
                None,
            ),
        )
    ],
    *[
        _action("robot." + action, label, "robot")
        for action, label in (
            ("enable", "Enable the robot"),
            ("disable", "Disable the robot"),
            ("clear_alarm", "Clear the robot alarm"),
            ("home", "Move the robot home"),
        )
    ],
    _action(
        "robot.move",
        "Move the robot to a pose",
        "robot",
        {
            "targets": obj({axis: number() for axis in ("x", "y", "z", "r")}),
            "speed": {
                "type": "integer",
                "minimum": 1,
                "description": "Percent speed, limited by the robot service cap.",
            },
        },
        command="execute",
    ),
    _action(
        "robot.jog",
        "Jog one robot axis",
        "robot",
        {
            "axis": string(enum=["x", "y", "z", "r"]),
            "direction": {"type": "integer", "enum": [-1, 1]},
            "step": number(exclusiveMinimum=0),
            "speed": {
                "type": "integer",
                "minimum": 1,
                "description": "Percent speed, limited by the robot service cap.",
            },
        },
        {"axis": "z", "direction": 1, "step": 0.5, "speed": 1},
        command="execute",
        revision=4,
    ),
    _action(
        "robot.vacuum",
        "Set robot suction",
        "robot",
        {"operation": string(enum=["suck", "off", "release"])},
        {"operation": "off"},
    ),
    _action(
        "robot.keyboard",
        "Control robot keyboard jog",
        "robot",
        reason="Keyboard jog requires the human Console deadman control.",
    ),
    _action(
        "microscope.select_objective",
        "Select an objective",
        "microscope",
        {"id": {"type": "integer", "enum": [0, 1, 2, 3]}},
        {"id": 0},
    ),
    _action(
        "microscope.objective_nudge",
        "Nudge the objective turret",
        "microscope",
        {"steps": {"type": "integer", "enum": [-10, -1, 1, 10]}},
        {"steps": 1},
    ),
    _action(
        "microscope.focus_move",
        "Move microscope focus",
        "microscope",
        {"steps": {"type": "integer", "minimum": -2147483648, "maximum": 2147483647}},
        {"steps": 1},
    ),
    _action(
        "microscope.autofocus",
        "Run autofocus",
        "microscope",
        {"mode": string(enum=["local", "full"])},
        {"mode": "local"},
    ),
    _action("microscope.set_focus_zero", "Set the focus zero", "microscope"),
    _action("microscope.home_focus", "Move focus to clearance", "microscope"),
    _action(
        "microscope.set_stabilization",
        "Set image stabilization",
        "microscope",
        {"enabled": {"type": "boolean"}},
        {"enabled": True},
    ),
    _action(
        "microscope.snapshot",
        "Save a microscope snapshot",
        "microscope",
        {"path": string(minLength=1, maxLength=512)},
        required=[],
    ),
    _action(
        "microscope.start_recording",
        "Start microscope recording",
        "microscope",
        RECORDING,
        required=["recording_id"],
    ),
    _action(
        "microscope.stop_recording",
        "Stop microscope recording",
        "microscope",
        {"recording_id": string(pattern="^[0-9a-f]{32}$")},
    ),
    _action(
        "microscope.record_with_sequence",
        "Record with a furnace sequence",
        "microscope",
        {**RECORDING, "enabled": {"type": "boolean"}},
        required=["recording_id", "enabled"],
    ),
    *[
        _action(
            "turret." + action,
            label,
            "microscope",
            fields,
            route="turret",
            command=action,
            required=required,
        )
        for action, label, fields, required in (
            ("begin", "Begin turret calibration", {"name": NAME, "new": {"type": "boolean"}}, []),
            (
                "record",
                "Record an objective image",
                {"objective_id": {"type": "integer", "enum": [0, 1, 2, 3]}},
                None,
            ),
            ("finish", "Finish turret calibration", {}, None),
            ("cancel", "Cancel turret calibration", {}, None),
            ("save", "Save the turret profile", {"name": NAME}, []),
            ("select", "Select a turret profile", {"id": IDENTIFIER}, None),
            ("load", "Load a turret profile", {"id": IDENTIFIER}, None),
            ("delete", "Delete a turret profile", {"id": IDENTIFIER}, None),
            (
                "upload",
                "Import a turret profile",
                {"profile": string(minLength=1, maxLength=2097152)},
                None,
            ),
        )
    ],
    _action("camera.turret_frame", "Read the simulated turret image", "microscope", route="frame"),
    _action("camera.thermal_connect", "Connect the thermal camera", "thermal", command="connect"),
    _action(
        "camera.thermal_shutter", "Calibrate the thermal shutter", "thermal", command="shutter"
    ),
    _action(
        "camera.set_region",
        "Save a thermal region",
        "thermal",
        reason="The current thermal contract does not expose region writes.",
    ),
    *[
        _action(
            "vision." + action,
            label,
            "vision",
            {"camera": {"type": "integer", "minimum": 1}},
            {"camera": 1},
        )
        for action, label in (("detect", "Detect samples"), ("read_markers", "Read camera markers"))
    ],
    _action(
        "vision.read_labels",
        "Read sample labels",
        "vision",
        {
            "camera": {"type": "integer", "minimum": 1},
            "region": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0},
                "minItems": 4,
                "maxItems": 4,
            },
        },
        {"camera": 1},
        required=["camera"],
    ),
    _action(
        "vision.calibrate_camera",
        "Calibrate camera coordinates",
        "vision",
        {
            "camera": {"type": "integer", "minimum": 1},
            "tag_points": array(array(number(), 3), 24),
            "tag_pixels": array(array(number(), 2), 24),
            "tray_points": array(array(number(), 3), 4, 2),
            "tray_pixels": array(array(number(), 2), 4, 2),
            "image_size": array({"type": "integer", "minimum": 1}, 2),
            "scan_r_deg": number(minimum=-1_000_000, maximum=1_000_000),
            "tool_dz_mm": number(minimum=-1_000_000, maximum=1_000_000),
        },
    ),
    _action(
        "vision.check_calibration",
        "Check camera calibration",
        "vision",
        {
            "camera": {"type": "integer", "minimum": 1},
            "flange_mm": array(number(minimum=-1_000_000, maximum=1_000_000), 4),
            "limit_px": number(exclusiveMinimum=0, maximum=3),
        },
        required=["camera", "flange_mm"],
    ),
    _action(
        "vision.observe_slots",
        "Observe tray slots",
        "vision",
        {
            "camera": {"type": "integer", "minimum": 1},
            "tray": {
                "type": "object",
                "description": "A tray model object. File paths are not accepted.",
            },
            "targets": {
                "type": "object",
                "minProperties": 1,
                "maxProperties": 128,
                "additionalProperties": {
                    "oneOf": [
                        {"type": "null"},
                        array(number(minimum=-1_000_000, maximum=1_000_000), 2),
                    ]
                },
                "description": "Map each slot ID to a taught robot XY pair in mm, or null.",
            },
            "axis": string(enum=["long", "short"]),
        },
    ),
    *[
        _action("furnace." + action, label, "furnace")
        for action, label in (
            ("connect", "Connect the furnace"),
            ("connect_flow", "Connect gas flow"),
            ("acknowledge", "Acknowledge the furnace fault"),
        )
    ],
    _action(
        "furnace.apply_setpoint",
        "Apply and run the furnace",
        "furnace",
        {"temperature_c": TEMPERATURE, "ramp_rate_c_per_min": RAMP},
        description=(
            "Apply a temperature and ramp rate within the furnace service caps. Starts heating."
        ),
    ),
    _action(
        "furnace.set_sequence", "Save the furnace sequence", "furnace", SEQUENCE, required=["steps"]
    ),
    _action(
        "furnace.start_sequence", "Start the furnace sequence", "furnace", SEQUENCE, required=[]
    ),
    _action(
        "furnace.set_gas_flow",
        "Set gas flow",
        "furnace",
        {
            "channel_index": {"type": "integer", "minimum": 0, "maximum": 2},
            "flow_percent": number(minimum=0, maximum=100),
        },
    ),
    _action(
        "furnace.set_gas_valve",
        "Set a gas valve",
        "furnace",
        {
            "channel_index": {"type": "integer", "minimum": 0, "maximum": 2},
            "open": {"type": "boolean"},
        },
    ),
    _action(
        "furnace.inspect_gas_flow",
        "Inspect gas flow",
        "furnace",
        {"channel_index": {"type": "integer", "minimum": 0, "maximum": 2}},
    ),
    _action(
        "furnace.operation",
        "Furnace operation",
        "furnace",
        reason="Choose a specific furnace action from the catalog.",
    ),
    _action(
        "recipe.run",
        "Run a recipe",
        "orchestrator",
        reason="Agent Connect has no reviewed recipe adapter.",
    ),
    _action(
        "composite.run",
        "Run a composite action",
        "orchestrator",
        reason="Agent Connect has no reviewed composite adapter.",
    ),
]

SPECS = {item["id"]: item for item in ACTION_SPECS}


def descriptors():
    """Return stable metadata without state, coordinates, or calibration values."""
    internal = {"route", "command", "disabled_reason"}
    return [
        {key: copy.deepcopy(value) for key, value in item.items() if key not in internal}
        for item in ACTION_SPECS
    ]


def validate(value, schema, path="params"):
    """Validate the small JSON Schema vocabulary used by the reviewed actions."""
    if "oneOf" in schema:
        matches = 0
        for choice in schema["oneOf"]:
            try:
                validate(value, choice, path)
                matches += 1
            except ValueError:
                pass
        if matches != 1:
            raise ValueError(f"{path} must match exactly one supported parameter shape.")
        return
    kind = schema.get("type")
    types = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "boolean": bool,
        "null": type(None),
    }
    if kind == "number":
        valid = type(value) in (int, float) and math.isfinite(value)
    else:
        valid = kind in types and type(value) is types[kind]
    if not valid:
        raise ValueError(f"{path} must be a finite JSON {kind}.")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must use a listed value.")
    if kind == "object":
        fields = schema.get("properties", {})
        if (schema.get("additionalProperties") is False and set(value) - set(fields)) or not set(
            schema.get("required", [])
        ) <= set(value):
            raise ValueError(f"{path} has missing or unsupported fields.")
        for key, item in value.items():
            if key in fields:
                validate(item, fields[key], path + "." + key)
            elif isinstance(schema.get("additionalProperties"), dict):
                validate(item, schema["additionalProperties"], path + "." + key)
        if len(value) < schema.get("minProperties", 0) or len(value) > schema.get(
            "maxProperties", math.inf
        ):
            raise ValueError(f"{path} has an invalid number of fields.")
    if kind == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
            raise ValueError(f"{path} has an invalid number of items.")
        for index, item in enumerate(value):
            if "items" in schema:
                validate(item, schema["items"], f"{path}[{index}]")
    if kind in {"number", "integer"}:
        if not math.isfinite(value):
            raise ValueError(f"{path} must be finite.")
        for key, valid in (
            ("minimum", lambda bound: value >= bound),
            ("maximum", lambda bound: value <= bound),
            ("exclusiveMinimum", lambda bound: value > bound),
        ):
            if key in schema and not valid(schema[key]):
                raise ValueError(f"{path} is outside its permitted range.")
    if kind == "string":
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 2097152):
            raise ValueError(f"{path} has an invalid length.")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise ValueError(f"{path} has an invalid format.")
