"""Control the MG400 from an interactive terminal without a simulator."""

import argparse
import curses
import shlex
import signal
import sys
import textwrap
from collections import deque
from pathlib import Path
from uuid import uuid4

from .config import finite_number
from .robot_config import load_robot_host
from .web_control import RobotPanelState
from .web_gripper import WebGripper

HELP = (
    "connect | disconnect | enable | disable | stop | status | help | quit",
    "move X Y Z R   Absolute flange pose: XYZ in mm, R in degrees; User=0, Tool=0.",
    "home           Move J1-J4 to zero degrees.",
    "grip 0..1000   Move only the gripper (0 closed, 1000 open).",
    "grip-init      Initialize the gripper. The jaws open. Do not hold an object.",
    "Ctrl+C: stop arm | Ctrl+D: quit | Up/Down: history | Left/Right: edit",
    "Stop is controlled stopping, not a physical emergency stop. It does not stop the gripper.",
)


def parse_command(line):
    """Validate one command before the session receives it."""
    words = shlex.split(line)
    if not words:
        return None
    name, *values = words
    actions = {
        "connect": "start_sync",
        "disconnect": "stop_sync",
        "enable": "enable",
        "disable": "disable",
        "stop": "stop",
        "home": "home",
        "grip-init": "initialize_gripper",
    }
    if name in actions and not values:
        return {"action": actions[name]}
    if name == "move" and len(values) == 4:
        return {
            "action": "execute",
            "targets": dict(
                zip(("x", "y", "z", "r"), (finite_number(float(v)) for v in values), strict=True)
            ),
        }
    if name == "grip" and len(values) == 1:
        position = finite_number(float(values[0]))
        if not position.is_integer() or not 0 <= position <= 1000:
            raise ValueError("Use grip with an integer from 0 to 1000")
        return {"action": "grip", "position": int(position)}
    if name in {"help", "status", "quit"} and not values:
        return name
    if words == ["quit", "--force"]:
        return "force_quit"
    raise ValueError("Unknown command or invalid arguments. Enter help for command syntax.")


def status_lines(state):
    """Display unavailable feedback without substituting zero measurements."""
    fresh = state["feedback_fresh"]

    def values(labels, measurements):
        measurements = measurements if fresh and measurements else [None] * len(labels)
        return "  ".join(
            f"{label}={value:.2f}" if value is not None else f"{label}=--"
            for label, value in zip(labels, measurements, strict=True)
        )

    connection = "LIVE" if fresh else "WAITING / STALE" if state["syncing"] else "DISCONNECTED"
    mode = state["robot_mode"] if fresh else "--"
    error = state["robot_error"] if fresh else "--"
    return [
        f"MG400  {state['robot_host']}  {connection}",
        f"Mode={mode}  Error={error}  Busy={state['busy']}  Locked={state['locked']}",
        "Joints (deg): " + values(("J1", "J2", "J3", "J4"), state["joints"]),
        "Flange (mm/deg): " + values(("X", "Y", "Z", "R"), state["actual"][:4]),
        "Gripper (0..1000): " + values(("position",), state["actual"][4:]),
        state["gripper_message"],
    ]


class TerminalSession:
    """Keep command input responsive while the shared controller runs its workers."""

    def __init__(self, state):
        self.state = state
        self.client_id = "terminal-" + uuid4().hex
        self.messages = deque(maxlen=80)
        self.last_message = None
        self.quitting = False
        self.finished = False
        self.exit_code = 0
        self.messages.extend(HELP)
        self.messages.append("Enter connect to read the robot. Enter enable before arm movement.")

    def tick(self):
        snapshot = self.state.heartbeat(self.client_id)
        if snapshot["message"] != self.last_message:
            self.messages.append(snapshot["message"])
            self.last_message = snapshot["message"]
        if self.quitting and not snapshot["busy"]:
            if not snapshot["syncing"] and not snapshot["stop_unconfirmed"]:
                self.finished = True
            else:
                self.quitting = False
                self.messages.append(
                    "Stop is unconfirmed. Use the physical emergency stop. "
                    "The session remains open."
                )
                self.messages.append(
                    "After a physical stop, quit --force exits without confirming robot rest."
                )
        return snapshot

    def submit(self, line):
        if line.strip():
            self.messages.append("mg400> " + line)
        try:
            command = parse_command(line)
            if command is None:
                return
            if command == "help":
                self.messages.extend(HELP)
            elif command == "status":
                self.messages.extend(status_lines(self.state.snapshot()))
            elif command == "force_quit":
                self.exit_code = 1
                self.finished = True
            elif command == "quit":
                snapshot = self.state.snapshot()
                if snapshot["stop_unconfirmed"]:
                    raise RuntimeError(
                        "Stop is unconfirmed. Use the physical emergency stop. "
                        "After a physical stop, quit --force exits without confirming robot rest."
                    )
                elif snapshot["syncing"] or snapshot["busy"]:
                    self.state.command({"action": "stop_sync"}, self.client_id)
                    self.quitting = True
                    self.messages.append("Waiting for confirmed arm stop and pending operations.")
                else:
                    self.finished = True
            else:
                if self.quitting and command["action"] not in {"stop", "disable"}:
                    raise RuntimeError("The session is closing. Wait for stop confirmation.")
                self.state.command(command, self.client_id)
        except (ValueError, OverflowError, RuntimeError, OSError) as error:
            self.messages.append(f"Error: {error}")


def run_terminal(screen, session):
    """Refresh feedback and process keys on the same thread as the control heartbeat."""
    curses.raw()
    screen.keypad(True)
    screen.timeout(100)
    line, cursor = "", 0
    history, history_index, draft = [], 0, ""

    while not session.finished:
        snapshot = session.tick()
        height, width = screen.getmaxyx()
        screen.erase()

        def write(row, text):
            if 0 <= row < height and width > 1:
                # Controller errors can contain control characters. Display only printable text.
                text = "".join(char if char.isprintable() else " " for char in text)
                try:
                    screen.addnstr(row, 0, text, width - 1)
                except curses.error:
                    pass  # A resize can invalidate the dimensions during this frame.

        if height >= 12 and width >= 40:
            for row, text in enumerate(status_lines(snapshot)):
                write(row, text)
            write(6, "-" * (width - 1))
            log = [
                part
                for message in session.messages
                for part in textwrap.wrap(message, max(1, width - 1))
            ]
            for row, text in enumerate(log[-(height - 9) :], start=7):
                write(row, text)
        else:
            write(0, "Resize to 40x12. Ctrl+C stops the arm.")
        prompt = "mg400> "
        visible = max(1, width - len(prompt) - 1)
        offset = max(0, cursor - visible)
        write(height - 1, prompt + line[offset : offset + visible])
        try:
            screen.move(height - 1, min(width - 1, len(prompt) + cursor - offset))
            screen.refresh()
            key = screen.get_wch()
        except curses.error:
            continue
        if key == "\x03":
            session.submit("stop")
            line, cursor = "", 0
        elif key == "\x04":
            session.submit("quit")
        elif key in ("\n", "\r", curses.KEY_ENTER):
            session.submit(line)
            if line.strip():
                history.append(line)
                history = history[-100:]
            history_index, draft = len(history), ""
            line, cursor = "", 0
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            if cursor:
                line = line[: cursor - 1] + line[cursor:]
                cursor -= 1
        elif key == curses.KEY_DC:
            line = line[:cursor] + line[cursor + 1 :]
        elif key == curses.KEY_LEFT:
            cursor = max(0, cursor - 1)
        elif key == curses.KEY_RIGHT:
            cursor = min(len(line), cursor + 1)
        elif key in (curses.KEY_HOME, "\x01"):
            cursor = 0
        elif key in (curses.KEY_END, "\x05"):
            cursor = len(line)
        elif key == "\x15":
            line, cursor = "", 0
        elif key in (curses.KEY_UP, curses.KEY_DOWN):
            if history_index == len(history):
                draft = line
            change = -1 if key == curses.KEY_UP else 1
            history_index = max(0, min(len(history), history_index + change))
            line = history[history_index] if history_index < len(history) else draft
            cursor = len(line)
        elif isinstance(key, str) and key.isascii() and key.isprintable() and len(line) < 512:
            line = line[:cursor] + key + line[cursor:]
            cursor += 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Interactive MG400 hardware control (no simulator)"
    )
    parser.add_argument("--robot-host", help="Robot address; default: config/robot.toml")
    parser.add_argument(
        "--gripper-feedback",
        type=Path,
        help="Feedback file from a local USB collector with --commands",
    )
    args = parser.parse_args(argv)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "Use an interactive terminal. For SSH, allocate a terminal with ssh -t.",
            file=sys.stderr,
        )
        return 2
    state = None
    exit_code = 0
    previous_handlers = {}

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    try:
        host = args.robot_host or load_robot_host(
            Path(__file__).resolve().parents[2] / "config/robot.toml"
        )
        if not host.strip() or any(char.isspace() for char in host):
            raise ValueError("The robot address must contain no whitespace")
        state = RobotPanelState(
            host, gripper=WebGripper(args.gripper_feedback) if args.gripper_feedback else None
        )
        session = TerminalSession(state)
        for signum in (signal.SIGTERM, signal.SIGHUP):
            previous_handlers[signum] = signal.signal(signum, interrupted)
        curses.wrapper(run_terminal, session)
        exit_code = session.exit_code
    except KeyboardInterrupt:
        print("Terminal interrupted. Requesting robot stop.", file=sys.stderr)
        exit_code = 130
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        exit_code = 1
    finally:
        try:
            if state is not None:
                # Repeated signals must not interrupt the bounded stop attempt.
                for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                    old_handler = signal.signal(signum, signal.SIG_IGN)
                    previous_handlers.setdefault(signum, old_handler)
                state.close()
                snapshot = state.snapshot()
                print(snapshot["message"], file=sys.stderr)
                print(snapshot["gripper_message"], file=sys.stderr)
                if snapshot["stop_unconfirmed"] or snapshot["busy"]:
                    exit_code = exit_code or 1
                    print(
                        "Robot rest is unconfirmed. Use the physical emergency stop.",
                        file=sys.stderr,
                    )
        except Exception as error:
            print(f"Cleanup failed: {error}. Use the physical emergency stop.", file=sys.stderr)
            exit_code = exit_code or 1
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
