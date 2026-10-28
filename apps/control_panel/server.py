"""Serve the independent MG400 browser panel.

Run with Python 3.11 or later. Robot mode can send hardware commands.
"""

import argparse
import hmac
import ipaddress
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mg400_agent.controller_ownership import ControllerOwnership  # noqa: E402
from mg400_agent.feedback import FeedbackConnection  # noqa: E402
from mg400_agent.robot_config import load_robot_host  # noqa: E402
from mg400_agent.web_control import RobotPanelState  # noqa: E402
from mg400_agent.web_gripper import WebGripper  # noqa: E402

ASSETS = Path(__file__).resolve().parent


def normalize_base_path(value):
    if (
        not value.startswith("/")
        or any(char in value for char in "?#\\")
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        raise ValueError("Base path must be an absolute URL path without dot segments or a query")
    return value.rstrip("/") + "/"


class ReadOnlyPanelState:
    """Read joint feedback without sending robot commands."""

    mode = "read-only"

    def __init__(self, robot_host, *, connection_factory=FeedbackConnection):
        if not robot_host:
            raise ValueError("Read-only mode requires a robot host")
        self.robot_host = robot_host
        self.syncing = False
        self.joints = None
        self.robot_mode = None
        self.robot_error = None
        self.received = None
        self.message = "Read-only mode. Start sync to read joint feedback."
        self._lock = threading.RLock()
        self._reader = None
        self._stop = threading.Event()
        self._connection_factory = connection_factory
        self._ownership = ControllerOwnership(robot_host)

    def snapshot(self):
        with self._lock:
            age = time.monotonic() - self.received if self.received is not None else None
            return {
                "mode": self.mode,
                "robot_host": self.robot_host,
                "enabled": None,
                "syncing": self.syncing,
                "actual": [None] * 5,
                "joints": list(self.joints) if self.joints is not None else None,
                "robot_mode": self.robot_mode,
                "robot_error": self.robot_error,
                "feedback_fresh": self.syncing and age is not None and age < 0.5,
                "message": self.message,
            }

    def command(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Request must be a JSON object")
        action = payload.get("action")
        if not isinstance(action, str) or action not in {
            "start_sync",
            "stop_sync",
            "enable",
            "disable",
            "home",
            "execute",
            "stop",
            "initialize_gripper",
            "keyboard",
        }:
            raise ValueError("Unknown action")
        if action == "keyboard":
            raise PermissionError("Keyboard control is unavailable in read-only mode")
        if set(payload) != ({"action", "targets"} if action == "execute" else {"action"}):
            raise ValueError("Unexpected command fields")
        with self._lock:
            if action == "start_sync":
                if not self.syncing:
                    if self._reader is not None and self._reader.is_alive():
                        raise RuntimeError("Feedback reader is stopping. Retry start sync.")
                    self._ownership.acquire()
                    self.syncing = True
                    self.received = None
                    self.message = "Connecting to the passive feedback stream."
                    self._stop = threading.Event()
                    try:
                        self._reader = threading.Thread(target=self._read_feedback, daemon=True)
                        self._reader.start()
                    except Exception:
                        self.syncing = False
                        self._reader = None
                        self._ownership.release()
                        raise
            elif action == "stop_sync":
                self._stop.set()
                self.syncing = False
                self.message = "Feedback sync stopped."
            else:
                raise PermissionError("Robot commands are unavailable in read-only mode")
            return self.snapshot()

    def _read_feedback(self):
        try:
            with self._connection_factory(self.robot_host, timeout_s=1.0) as connection:
                while not self._stop.is_set():
                    sample = connection.read_latest()
                    with self._lock:
                        if self._stop.is_set():
                            return
                        self.joints = sample.joints_deg
                        self.robot_mode = sample.mode
                        self.robot_error = sample.error_status
                        self.received = time.monotonic()
                        self.message = (
                            "Live joint feedback. Pose and gripper feedback are unavailable."
                        )
        except (OSError, ValueError) as error:
            with self._lock:
                if not self._stop.is_set():
                    self.syncing = False
                    self.message = f"Feedback unavailable: {error}"
        finally:
            with self._lock:
                self.syncing = False
                self._ownership.release()

    def close(self):
        self._stop.set()
        with self._lock:
            self.syncing = False
        if self._reader is not None:
            self._reader.join(timeout=2)


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, state, token=None, base_path="/"):
        self.state = state
        self.token = token
        self.base_path = normalize_base_path(base_path)
        super().__init__(address, PanelHandler)


class PanelHandler(BaseHTTPRequestHandler):
    # Keep-alive avoids a new TCP connection for each key update. Without Nagle,
    # small responses do not wait for the client's delayed acknowledgement.
    protocol_version = "HTTP/1.1"
    timeout = 15
    disable_nagle_algorithm = True

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        data = json.dumps(body, allow_nan=False).encode() if isinstance(body, dict) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status >= 400:
            # An early error can leave the request body unread on this connection.
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        expected = self.server.token
        if expected and not hmac.compare_digest(
            self.headers.get("Authorization", ""), f"Bearer {expected}"
        ):
            self._send(401, {"error": "Enter the panel access token"})
            return False
        return True

    def do_GET(self):
        # An unread body would be parsed as the next request on this connection.
        length = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding") is not None or length not in (None, "0"):
            self._send(400, {"error": "GET requests must not have a body"})
            return
        path = urlsplit(self.path).path
        base = self.server.base_path.rstrip("/")
        if base and path == base:
            self.send_response(308)
            self.send_header("Location", self.server.base_path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path = path[len(base) :] if path.startswith(base + "/") else None
        if path in {"/", "/index.html"}:
            self._send(200, (ASSETS / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            if self._authorized():
                self._send(200, self.server.state.snapshot())
        else:
            self._send(404, {"error": "Not found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in {
            self.server.base_path + "api/command",
            self.server.base_path + "api/heartbeat",
            self.server.base_path + "api/keys",
        }:
            self._send(404, {"error": "Not found"})
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._send(400, {"error": "Send the request body with Content-Length"})
            return
        if not self._authorized():
            return
        if (
            self.headers.get("Content-Type", "").split(";")[0] != "application/json"
            or self.headers.get("X-MG400-Panel") != "1"
            or self.headers.get("Sec-Fetch-Site") == "cross-site"
        ):
            self._send(403, {"error": "Use the panel to send commands"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Request body must contain 1 to 4096 bytes")
            payload = json.loads(self.rfile.read(length))
            state = self.server.state
            if state.mode == "robot":
                client = self.headers.get("X-MG400-Client", "")
                if not client or len(client) > 128 or not client.isascii():
                    raise ValueError("Open the panel to identify this browser")
                if path.endswith("/heartbeat"):
                    if payload != {}:
                        raise ValueError("Heartbeat must be an empty JSON object")
                    result = state.heartbeat(client)
                elif path.endswith("/keys"):
                    result = state.keys(payload, client)
                else:
                    result = state.command(payload, client_id=client)
            elif path.endswith("/heartbeat"):
                raise ValueError("Heartbeat is only available in robot mode")
            elif path.endswith("/keys"):
                raise PermissionError("Keyboard control is unavailable in read-only mode")
            else:
                result = state.command(payload)
        except (ValueError, UnicodeError) as error:
            self._send(400, {"error": str(error)})
        except PermissionError as error:
            self._send(403, {"error": str(error)})
        except RuntimeError as error:
            self._send(409, {"error": str(error)})
        else:
            self._send(200, result)

    def log_message(self, format, *args):
        # Keep tokens and request bodies out of the request log.
        if format.startswith("Request timed out"):
            return
        polls = (
            f"GET {self.server.base_path}api/state ",
            f"POST {self.server.base_path}api/heartbeat ",
            f"POST {self.server.base_path}api/keys ",
        )
        if (
            len(args) > 1
            and isinstance(args[0], str)
            and args[0].startswith(polls)
            and (str(args[1]) == "200")
        ):
            return
        super().log_message(format, *args)


def report(text, *, wait=None):
    """Write a message to stderr if it works. Output must never skip or delay a stop.

    With wait, write on a helper thread and continue after at most wait seconds.
    """

    def write():
        try:
            print(text, file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass

    if wait is None:
        write()
        return
    try:
        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        writer.join(wait)
    except RuntimeError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--mode", choices=("robot", "read-only"), default="robot")
    parser.add_argument("--base-path", type=normalize_base_path, default="/")
    parser.add_argument("--robot-host", help="Robot address. Default: config/robot.toml")
    parser.add_argument(
        "--gripper-feedback",
        type=Path,
        help="Feedback file from a --commands USB collector on this server's host",
    )
    args = parser.parse_args(argv)
    token = os.environ.get("MG400_PANEL_TOKEN")
    try:
        loopback = ipaddress.ip_address(args.bind).is_loopback
    except ValueError:
        loopback = args.bind == "localhost"
    if not loopback and (not token or len(token) < 24):
        parser.error(
            "Set MG400_PANEL_TOKEN to at least 24 characters before binding outside localhost"
        )
    host = args.robot_host
    if args.gripper_feedback and args.mode != "robot":
        parser.error("--gripper-feedback requires --mode robot")
    if not host:
        host = load_robot_host(ROOT / "config/robot.toml")
    state = (
        RobotPanelState(
            host,
            gripper=WebGripper(args.gripper_feedback) if args.gripper_feedback else None,
        )
        if args.mode == "robot"
        else ReadOnlyPanelState(host)
    )
    server = PanelServer((args.bind, args.port), state, token, args.base_path)
    print(
        f"MG400 panel: http://{args.bind}:{server.server_port}{args.base_path} ({args.mode})",
        flush=True,
    )
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {signum: signal.getsignal(signum) for signum in signals}

    def interrupted(signum, frame):
        # Repeated signals must not interrupt the bounded stop attempt.
        for number in signals:
            signal.signal(number, signal.SIG_IGN)
        raise KeyboardInterrupt

    # Ctrl+C, systemctl stop and a closed SSH session must all stop a running jog.
    for signum in signals:
        if previous[signum] != signal.SIG_IGN:
            signal.signal(signum, interrupted)
    exit_code = 0
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for signum in signals:
            signal.signal(signum, signal.SIG_IGN)
        try:
            # A hung-up terminal or a closed pipe must not prevent the stop.
            report("Stopping robot control before exit.", wait=0.2)
            try:
                server.server_close()
            finally:
                state.close()
            snapshot = state.snapshot()
            report(snapshot.get("message", ""))
            if snapshot.get("stop_unconfirmed") or snapshot.get("busy"):
                exit_code = 1
                report("Robot rest is unconfirmed. Use the physical emergency stop.")
        except Exception as error:
            exit_code = 1
            report(f"Cleanup failed: {error}. Use the physical emergency stop.")
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
