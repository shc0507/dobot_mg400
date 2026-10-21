"""Serve the independent MG400 demo panel or passive robot feedback.

Run with Python 3.11 or later. This server has no hardware command path.
"""

import argparse
import hmac
import ipaddress
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mg400_agent.config import Config, Pose, finite_number  # noqa: E402
from mg400_agent.feedback import FeedbackConnection  # noqa: E402
from mg400_agent.robot_config import load_robot_host  # noqa: E402

ASSETS = Path(__file__).resolve().parent
AXES = ("x", "y", "z", "r", "grip")


def normalize_base_path(value):
    if (
        not value.startswith("/") or any(char in value for char in "?#\\")
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        raise ValueError("Base path must be an absolute URL path without dot segments or a query")
    return value.rstrip("/") + "/"


class PanelState:
    """Keep commands serial and take consistent snapshots for HTTP readers."""

    def __init__(self, mode="demo", robot_host=None, *, connection_factory=FeedbackConnection):
        if mode not in {"demo", "read-only"}:
            raise ValueError("Mode must be demo or read-only")
        if mode == "read-only" and not robot_host:
            raise ValueError("Read-only mode requires a robot host")
        self.mode = mode
        self.robot_host = robot_host
        self.config = Config.load(ROOT / "config/demo.toml")
        self.minimum = (*self.config.workspace.minimum.values(), 0)
        self.maximum = (*self.config.workspace.maximum.values(), 1000)
        self.actual = [*self.config.home.values(), 1000] if mode == "demo" else [None] * 5
        self.enabled = False if mode == "demo" else None
        self.syncing = False
        self.joints = None
        self.robot_mode = None
        self.robot_error = None
        self.received = None
        self.message = "Demo ready. No robot is connected." if mode == "demo" else (
            "Read-only mode. Start sync to read joint feedback."
        )
        self._lock = threading.RLock()
        self._reader = None
        self._stop = threading.Event()
        self._connection_factory = connection_factory

    def snapshot(self):
        with self._lock:
            age = time.monotonic() - self.received if self.received is not None else None
            return {
                "mode": self.mode,
                "robot_host": self.robot_host,
                "enabled": self.enabled,
                "syncing": self.syncing,
                "actual": list(self.actual),
                "joints": list(self.joints) if self.joints is not None else None,
                "robot_mode": self.robot_mode,
                "robot_error": self.robot_error,
                "feedback_fresh": self.syncing and age is not None and age < 0.5,
                "message": self.message,
                "limits": {key: [lo, hi] for key, lo, hi in zip(
                    AXES, self.minimum, self.maximum, strict=True
                )} if self.mode == "demo" else None,
                "home": [*self.config.home.values(), 1000] if self.mode == "demo" else None,
            }

    def _targets(self, payload):
        values = payload.get("targets")
        if not isinstance(values, dict) or set(values) != set(AXES):
            raise ValueError("Targets must contain x, y, z, r, and grip")
        try:
            target = [finite_number(values[axis]) for axis in AXES]
        except OverflowError as error:
            raise ValueError("Targets must be finite numbers") from error
        self.config.workspace.validate(Pose(*target[:4]))
        if not 0 <= target[4] <= 1000 or not target[4].is_integer():
            raise ValueError("Gripper target must be an integer from 0 to 1000")
        return target

    def command(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Request must be a JSON object")
        action = payload.get("action")
        if not isinstance(action, str) or action not in {
            "start_sync", "stop_sync", "enable", "disable", "home", "execute"
        }:
            raise ValueError("Unknown action")
        if set(payload) != ({"action", "targets"} if action == "execute" else {"action"}):
            raise ValueError("Unexpected command fields")
        with self._lock:
            if action == "start_sync":
                if not self.syncing:
                    if self._reader is not None and self._reader.is_alive():
                        raise RuntimeError("Feedback reader is stopping. Retry start sync.")
                    self.syncing = True
                    self.received = None
                    self.message = "Demo sync started." if self.mode == "demo" else (
                        "Connecting to the passive feedback stream."
                    )
                    if self.mode == "read-only":
                        self._stop = threading.Event()
                        self._reader = threading.Thread(target=self._read_feedback, daemon=True)
                        self._reader.start()
            elif action == "stop_sync":
                self._stop.set()
                self.syncing = False
                self.message = "Feedback sync stopped."
            else:
                if self.mode != "demo":
                    raise PermissionError("Robot commands are unavailable in read-only mode")
                if action == "enable":
                    self.enabled = True
                    self.message = "Demo robot enabled."
                elif action == "disable":
                    self.enabled = False
                    self.message = "Demo robot disabled."
                else:
                    if not self.enabled:
                        raise RuntimeError("Enable the demo robot before moving")
                    target = self._targets(payload) if action == "execute" else (
                        [*self.config.home.values(), self.actual[4]]
                    )
                    self.actual = target
                    self.message = "Demo targets applied." if action == "execute" else (
                        "Demo robot returned to the configured demo home."
                    )
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
    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        data = json.dumps(body, allow_nan=False).encode() if isinstance(body, dict) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
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
        path = urlsplit(self.path).path
        base = self.server.base_path.rstrip("/")
        if base and path == base:
            self.send_response(308)
            self.send_header("Location", self.server.base_path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path = path[len(base):] if path.startswith(base + "/") else None
        if path in {"/", "/index.html"}:
            self._send(200, (ASSETS / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            if self._authorized():
                self._send(200, self.server.state.snapshot())
        else:
            self._send(404, {"error": "Not found"})

    def do_POST(self):
        if urlsplit(self.path).path != self.server.base_path + "api/command":
            self._send(404, {"error": "Not found"})
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
            result = self.server.state.command(payload)
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
        if args and isinstance(args[0], str) and args[0].startswith("GET /api/state "):
            return
        super().log_message(format, *args)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--mode", choices=("demo", "read-only"), default="demo")
    parser.add_argument("--base-path", type=normalize_base_path, default="/")
    parser.add_argument("--robot-host", help="Read-only feedback host. No commands are sent.")
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
    if args.mode == "read-only" and not host:
        host = load_robot_host(ROOT / "config/robot.toml")
    state = PanelState(args.mode, host)
    server = PanelServer((args.bind, args.port), state, token, args.base_path)
    print(
        f"MG400 panel: http://{args.bind}:{server.server_port}{args.base_path} ({args.mode})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        state.close()


if __name__ == "__main__":
    main()
