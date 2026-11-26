"""labctl device groups for agents. Each call prints 1 JSON object on 1 line to stdout.

Exit codes: 0 ok, 1 refused or failed, 2 usage, 3 outcome unknown, 130 interrupted.
"""

import argparse
import json
import math
import secrets
import signal
import sys
from contextlib import contextmanager

from . import collision
from .agent import (
    DEFAULT_TIMEOUT_S,
    DEFAULT_URL,
    DO_TIMEOUT_S,
    Agent,
    Unreachable,
    exit_code,
    failure,
    parse_params,
)

DEVICES = ("robot", "stage", "furnace", "microscope", "vision", "thermal")
GROUPS = frozenset({*DEVICES, "camera", "all", "do", "chip"})
# Shortcuts: name -> (action, fixed params).
ROBOT_PLAIN = {
    "enable": "enable",
    "disable": "disable",
    "home": "home",
    "clear-alarm": "clear_alarm",
}


POSE_WAIT_S = 2.0


class UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


@contextmanager
def _interrupts():
    """SIGINT, SIGTERM and SIGHUP raise KeyboardInterrupt: a wait sends 1 device stop."""
    previous = {}

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    try:
        for signum in (
            getattr(signal, name)
            for name in ("SIGINT", "SIGTERM", "SIGHUP")
            if hasattr(signal, name)
        ):
            previous[signum] = signal.signal(signum, interrupt)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _parser():
    # Options work before and after the verb. The verb level must not reset them.
    common, late = _Parser(add_help=False), _Parser(add_help=False)
    for item, default in ((common, None), (late, argparse.SUPPRESS)):
        item.add_argument(
            "--session",
            default="main" if default is None else default,
            help="Agent session name (page agent-NAME)",
        )
        item.add_argument("--url", default=DEFAULT_URL if default is None else default)
    send = _Parser(add_help=False)
    send.add_argument("--no-wait", action="store_true", help="Return after the command is sent")
    send.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, metavar="S")

    parser = _Parser(prog="labctl", description=__doc__, allow_abbrev=False)
    groups = parser.add_subparsers(dest="group", required=True)

    def group(name):
        sub = groups.add_parser(name, parents=[common], allow_abbrev=False)
        return sub.add_subparsers(dest="verb", required=True)

    def verb(verbs, name, *, sends=False):
        return verbs.add_parser(name, parents=[late, send] if sends else [late], allow_abbrev=False)

    for device in DEVICES:
        verbs = group(device)
        verb(verbs, "state")
        verb(verbs, "describe")
        verb(verbs, "take")
        verb(verbs, "release")
        verb(verbs, "stop")
        wait = verb(verbs, "wait")
        wait.add_argument("command_id", nargs="?")
        wait.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, metavar="S")
        call = verb(verbs, "call", sends=True)
        call.add_argument("action")
        call.add_argument("params", nargs="*", metavar="NAME=VALUE")
        if device == "robot":
            for name in ROBOT_PLAIN:
                verb(verbs, name, sends=True)
            move = verb(verbs, "move", sends=True)
            for axis in "XYZR":
                move.add_argument(axis.lower(), type=float, metavar=axis)
            move.add_argument("--speed", type=int)
            move.add_argument("--relative", action="store_true")
            move.add_argument(
                "--no-collision-check",
                action="store_true",
                help="Send the move without the bench twin check (a taught, supervised move)",
            )
            vacuum = verb(verbs, "vacuum", sends=True)
            vacuum.add_argument("operation", choices=["suck", "release", "off"])
        elif device == "stage":
            move = verb(verbs, "move", sends=True)
            move.add_argument("motor")
            move.add_argument("rotations", type=float)
            move.add_argument("--speed", type=float, default=0.25)
            calibrate = verb(verbs, "calibrate", sends=True)
            calibrate.add_argument("motor")
            calibrate.add_argument("--speed", type=float, default=0.25)
            for name in (
                "reset-position",
                "set-limit",
                "clear-upper-limit",
                "clear-lower-limit",
                "clear-fault",
            ):
                verb(verbs, name, sends=True).add_argument("motor")
            goto = verb(verbs, "goto", sends=True)
            goto.add_argument("motor")
            goto.add_argument("position", type=float)
            goto.add_argument("--speed", type=float, default=0.25)
            verb(verbs, "connect").add_argument("port", nargs="?")
        elif device == "furnace":
            setpoint = verb(verbs, "setpoint", sends=True)
            setpoint.add_argument("temperature_c", type=float, metavar="C")
            setpoint.add_argument("--ramp", type=float, required=True, metavar="C_PER_MIN")
            sequence = verb(verbs, "sequence", sends=True)
            sequence.add_argument("file")
            sequence.add_argument("--start", action="store_true")
            gas = verb(verbs, "gas", sends=True)
            gas.add_argument("channel", type=int)
            gas.add_argument("percent", type=float)
            valve = verb(verbs, "valve", sends=True)
            valve.add_argument("channel", type=int)
            valve.add_argument("position", choices=["open", "close"])
            verb(verbs, "acknowledge")
            verb(verbs, "gas-off").add_argument("channel", type=int, nargs="?", choices=[0, 1, 2])
        elif device == "microscope":
            verb(verbs, "objective", sends=True).add_argument("id", type=int)
            verb(verbs, "nudge", sends=True).add_argument("steps", type=int)
            verb(verbs, "focus", sends=True).add_argument("steps", type=int)
            verb(verbs, "autofocus", sends=True).add_argument("mode", choices=["full", "local"])
            verb(verbs, "zero", sends=True)
            verb(verbs, "home", sends=True)
            record = verb(verbs, "record", sends=True)
            record.add_argument("operation", choices=["start", "stop"])
            record.add_argument("recording_id", nargs="?")
            record.add_argument("--mode", choices=["time_lapse", "live"])
            record.add_argument("--name")
            frame = verb(verbs, "frame")
            frame.add_argument("camera", choices=["microscope", "side"])
            frame.add_argument("--png", action="store_true")
            frame.add_argument("--out")
        elif device == "vision":
            for name in ("detect", "markers", "labels"):
                verb(verbs, name, sends=True).add_argument("camera", type=int)
            locate = verb(verbs, "locate", sends=True)
            locate.add_argument("what", choices=["markers", "chips"])
            locate.add_argument("camera", type=int)
            locate.add_argument("touch_z", type=float)

    verbs = group("camera")
    still = verb(verbs, "still")
    still.add_argument("view")
    still.add_argument("--out")

    do = groups.add_parser("do", parents=[common], allow_abbrev=False)
    do.add_argument("recipe")
    do.add_argument("params", nargs="*", metavar="NAME=VALUE")
    do.add_argument("--timeout", type=float, default=DO_TIMEOUT_S, metavar="S")
    verbs = group("chip")
    verb(verbs, "locate").add_argument("slot")

    verbs = group("all")
    verb(verbs, "state")
    events = verb(verbs, "events")
    events.add_argument("--since", type=int, default=0)
    events.add_argument("--device", choices=[*DEVICES, "runs"])
    history = verb(verbs, "history")
    history.add_argument("--device", choices=DEVICES)
    history.add_argument("--from", type=float, dest="start", metavar="UNIX_TIME")
    history.add_argument("--to", type=float, dest="end", metavar="UNIX_TIME")
    verb(verbs, "stop")
    verb(verbs, "release")
    verb(verbs, "session")
    return parser


def robot_state(agent):
    """Return (robot state with a known ``actual`` pose, None), or (None, failure)."""
    # The pose is unknown while the arm moves and until the next feedback sample.
    deadline = agent.clock() + POSE_WAIT_S
    while True:
        current = agent.state("robot")
        if not current.get("ok"):
            return None, current
        state = current.get("state") or {}
        actual = state.get("actual")
        if isinstance(actual, list) and len(actual) == 4 and None not in actual:
            return state, None
        if agent.clock() >= deadline:
            return None, failure(
                "busy", "The current pose is unknown while the arm moves.", device="robot"
            )
        agent.sleep(0.1)


def current_pose(agent):
    """Return (flange pose, None), or (None, failure) when the robot gives no pose."""
    state, failed = robot_state(agent)
    return (None, failed) if failed else (state["actual"], None)


def _collision(agent, state, target):
    """Return a failure dict when the bench twin does not clear the straight move to target."""
    skip = "For a taught, supervised move, add --no-collision-check."
    try:
        problem = collision.check(
            state["actual"], target, joints=state.get("joints"), stage=agent.twin_joints()
        )
    except collision.Unavailable as error:
        message = f"The collision check cannot run: {error} No command was sent. {skip}"
        return failure("collision", message, device="robot")
    if problem is None:
        return None
    place = "{along_mm:g} mm along the line, at flange ({pose[0]:g}, {pose[1]:g}, {pose[2]:g})"
    where = f"'{problem['obstacle']}' ({problem['group']}) " + place.format(**problem)
    if problem["clearance_mm"] == 0:
        text = f"The bench twin shows contact of the arm ({problem['arm_part']}) with {where}."
    elif problem["start_clearance_mm"] is None:
        text = (
            f"The bench twin shows the arm ({problem['arm_part']}) {problem['clearance_mm']:g} mm "
            f"from {where}. The margin is {collision.MARGIN_MM:g} mm."
        )
    else:
        text = (
            f"The bench twin shows the arm ({problem['arm_part']}) {problem['clearance_mm']:g} mm "
            f"from {where}. At the start it is {problem['start_clearance_mm']:g} mm away. "
            f"Inside the {collision.MARGIN_MM:g} mm margin, a move must not go closer."
        )
    if problem.get("swept"):
        text += (
            " The stage position is unknown (no Auto calibrate), so the check uses the full"
            " stage travel."
        )
    return failure(
        "collision", f"{text} No command was sent. {skip}", device="robot", collision=problem
    )


def _shortcut(agent, args):
    """Return (action, params) for a command shortcut, or a failure dict."""
    group, verb = args.group, args.verb
    if group == "robot":
        if verb in ROBOT_PLAIN:
            return ROBOT_PLAIN[verb], {}
        if verb == "vacuum":
            return "vacuum", {"operation": args.operation}
        if verb == "move":
            target = [args.x, args.y, args.z, args.r]
            if args.relative or not args.no_collision_check:
                state, failed = robot_state(agent)
                if failed:
                    return failed
                actual = state["actual"]
            if args.relative:
                target = [a + b for a, b in zip(actual, target, strict=True)]
            if not args.no_collision_check:
                blocked = _collision(agent, state, target)
                if blocked:
                    return blocked
            params = {"targets": dict(zip("xyzr", target, strict=True))}
            if args.speed is not None:
                params["speed"] = args.speed
            return "execute", params
    if group == "stage":
        if verb in {"move", "goto", "calibrate"} and (
            not math.isfinite(args.speed) or args.speed <= 0
        ):
            raise UsageError("SPEED must be a positive finite rotation rate.")
        if verb == "move":
            if not math.isfinite(args.rotations) or args.rotations == 0:
                raise UsageError("ROTATIONS must be finite and must not be 0.")
            return "move", {
                "motor": args.motor,
                "direction": "ccw" if args.rotations > 0 else "cw",
                "rotations": abs(args.rotations),
                "speed": args.speed,
            }
        if verb == "calibrate":
            return "calibrate", {"motor": args.motor, "speed": args.speed}
        if verb in {
            "reset-position",
            "set-limit",
            "clear-upper-limit",
            "clear-lower-limit",
            "clear-fault",
        }:
            return verb.replace("-", "_"), {"motor": args.motor}
        if verb == "goto":
            if not math.isfinite(args.position):
                raise UsageError("POSITION must be finite rotations.")
            return "move_to_position", {
                "motor": args.motor,
                "position": args.position,
                "speed": args.speed,
            }
    if group == "furnace":
        if verb == "setpoint":
            return "apply_setpoint", {
                "temperature_c": args.temperature_c,
                "ramp_rate_c_per_min": args.ramp,
            }
        if verb == "sequence":
            params = parse_params([f"value=@{args.file}"], sys.stdin)["value"]
            if not isinstance(params, dict):
                raise UsageError("The sequence file must hold a JSON object.")
            return ("start_sequence" if args.start else "set_sequence"), params
        if verb == "gas":
            return "set_gas_flow", {"channel_index": args.channel, "flow_percent": args.percent}
        if verb == "valve":
            return "set_gas_valve", {"channel_index": args.channel, "open": args.position == "open"}
    if group == "microscope":
        if verb == "objective":
            return "select_objective", {"id": args.id}
        if verb == "nudge":
            return "objective_nudge", {"steps": args.steps}
        if verb == "focus":
            return "focus_move", {"steps": args.steps}
        if verb == "autofocus":
            return "autofocus", {"mode": args.mode}
        if verb == "zero":
            return "set_focus_zero", {}
        if verb == "home":
            return "home_focus", {}
        if verb == "record":
            if args.operation == "stop":
                if not args.recording_id:
                    raise UsageError("record stop needs the RECORDING_ID.")
                return "stop_recording", {"recording_id": args.recording_id}
            params = {"recording_id": args.recording_id or secrets.token_hex(16)}
            if args.mode:
                params["mode"] = args.mode
            if args.name:
                params["run_name"] = args.name
            return "start_recording", params
    if group == "vision":
        if verb == "locate":
            # The wrist camera moves with the arm: its frame needs the flange pose.
            actual, failed = current_pose(agent)
            if failed:
                return failed
            params = {"camera": args.camera, "flange_mm": actual, "touch_z_mm": args.touch_z}
            return ("read_markers" if args.what == "markers" else "detect"), params
        action = {"detect": "detect", "markers": "read_markers", "labels": "read_labels"}[verb]
        return action, {"camera": args.camera}
    raise UsageError(f"Unknown command: {group} {verb}")


def dispatch(agent, args):
    group, verb = args.group, getattr(args, "verb", None)
    if group == "all":
        return {
            "state": agent.all_state,
            "stop": agent.stop_all,
            "release": agent.release_all,
            "session": agent.session_report,
            "events": lambda: agent.events(args.since, args.device),
            "history": lambda: agent.history(args.device, args.start, args.end),
        }[verb]()
    if group == "camera":
        return agent.camera_still(args.view, args.out)
    if group == "do":
        if args.timeout <= 0:
            raise UsageError("--timeout must be positive.")
        return agent.do(args.recipe, parse_params(args.params, sys.stdin), timeout=args.timeout)
    if group == "chip":
        return agent.chip_locate(args.slot)
    reads = {
        "state": lambda: agent.state(group),
        "describe": lambda: agent.describe(group),
        "take": lambda: agent.take(group),
        "release": lambda: agent.release(group),
        "stop": lambda: agent.stop(group),
        "wait": lambda: agent.wait(group, args.command_id, timeout=args.timeout),
    }
    if verb in reads:
        return reads[verb]()
    if group == "stage" and verb == "connect":
        if not args.port:
            return agent.stage_ports()
        return agent.call("stage", "connect", {"port": args.port})
    if group == "furnace" and verb == "acknowledge":
        return agent.call("furnace", "acknowledge", {})
    if group == "furnace" and verb == "gas-off":
        return agent.gas_off(args.channel)
    if group == "microscope" and verb == "frame":
        return agent.microscope_frame(args.camera, args.png, args.out)
    if verb == "call":
        action, params = args.action, parse_params(args.params, sys.stdin)
    else:
        shortcut = _shortcut(agent, args)
        if isinstance(shortcut, dict):
            return shortcut
        action, params = shortcut
    if args.timeout <= 0:
        raise UsageError("--timeout must be positive.")
    return agent.call(group, action, params, wait=not args.no_wait, timeout=args.timeout)


def main(argv=None, *, out=None, agent_factory=Agent):
    out = sys.stdout if out is None else out
    try:
        args = _parser().parse_args(argv)
        agent = agent_factory(args.url, args.session)
        with _interrupts():
            result = dispatch(agent, args)
    except UsageError as error:
        result = failure("usage", str(error))
    except (ValueError, OSError) as error:
        result = failure("usage", str(error))
    except KeyboardInterrupt:
        result = failure("interrupted", "Interrupted before a command was sent.")
    except Unreachable as error:
        result = failure("unknown", f"{error} Read the state before you repeat the command.")
    except Exception as error:  # Keep the contract: always print 1 JSON object.
        result = failure("unknown", f"labctl failed: {error!r}")
    print(json.dumps(result, ensure_ascii=False, allow_nan=False), file=out, flush=True)
    return exit_code(result)
