"""Serve the MG400 robot service API: state and the instrument contract routes.

Run with Python 3.12 or later. Robot mode can send hardware commands.
"""

import argparse
import json
import logging
import logging.handlers
import queue
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from labkit import ports  # noqa: E402
from mg400.controller_ownership import ControllerOwnership  # noqa: E402
from mg400.feedback import FeedbackConnection  # noqa: E402
from mg400.robot_config import load_robot_host  # noqa: E402
from mg400.web_control import RobotPanelState  # noqa: E402

MAX_SEQUENCE = 2**53
# Robot speed cap: SpeedL and AccL percentages for every motion command.
MAX_SPEED_L = 20
MAX_ACC_L = 20


def normalize_base_path(value):
    if (
        not value.startswith("/")
        or any(char in value for char in "?#\\")
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        raise ValueError("Base path must be an absolute URL path without dot segments or a query")
    return value.rstrip("/") + "/"


def valid_client(value):
    """Accept 1 to 128 printable ASCII characters. The ID appears in the service log."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and value.isascii()
        and value.isprintable()
        and bool(value.strip())
    )


def refusal_status(error):
    """Return the HTTP status for a refusal, or None for an unexpected failure."""
    if isinstance(error, ValueError):
        return 400
    if isinstance(error, PermissionError):
        return 403
    if isinstance(error, RuntimeError):
        return 409
    return None


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
                "actual": [None] * 4,
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
            "keyboard",
            "clear_alarm",
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
                        self.message = "Live joint feedback. Pose feedback is unavailable."
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

    def __init__(self, address, state, base_path="/"):
        self.state = state
        self.base_path = normalize_base_path(base_path)
        super().__init__(address, PanelHandler)

    def report_error(self, text):
        # A blocked stderr must not hold a connection or the state lock.
        report(text, wait=0)


class PanelHandler(BaseHTTPRequestHandler):
    # Keep-alive avoids a new TCP connection for each key update. Without Nagle,
    # small responses do not wait for the client's delayed acknowledgement.
    protocol_version = "HTTP/1.1"
    timeout = 15
    disable_nagle_algorithm = True

    def _send(self, status, body, content_type="application/json; charset=utf-8", headers=()):
        data = json.dumps(body, allow_nan=False).encode() if isinstance(body, dict) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in headers:
            self.send_header(name, value)
        if status >= 400:
            # An early error can leave the request body unread on this connection.
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        # An unread body would be parsed as the next request on this connection.
        length = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding") is not None or length not in (None, "0"):
            self._send(400, {"error": "GET requests must not have a body"})
            return
        path = urlsplit(self.path).path
        base = self.server.base_path.rstrip("/")
        path = path[len(base) :] if path.startswith(base + "/") else None
        if path == "/api/state":
            self._send(200, self.server.state.snapshot())
        elif path in {"/api/describe", "/api/events"}:
            if self.server.state.mode != "robot":
                self._send(404, {"error": "Contract routes are only available in robot mode"})
                return
            try:
                if path == "/api/describe":
                    result = self.server.state.describe()
                else:
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    values = query.get("since", ["0"])
                    if len(values) != 1 or not values[0].isascii() or not values[0].isdecimal():
                        raise ValueError("Event sequence must be a nonnegative integer")
                    result = self.server.state.events.since(int(values[0]))
            except ValueError as error:
                self._send(400, {"error": str(error)})
            else:
                self._send(200, result)
        else:
            self._send(404, {"error": "Not found"})

    def do_POST(self):
        self._quiet = False
        path = urlsplit(self.path).path
        base = self.server.base_path
        route = path[len(base) :] if path.startswith(base) else None
        if route not in {"api/command", "api/heartbeat", "api/renew", "api/release", "api/stop"}:
            self._send(404, {"error": "Not found"})
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._send(400, {"error": "Send the request body with Content-Length"})
            return
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self._send(415, {"error": "Expected Content-Type: application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Request body must contain 1 to 4096 bytes")
            payload = json.loads(self.rfile.read(length))
            status, result = self._contract_post(self.server.state, route, payload)
        except (ValueError, PermissionError, RuntimeError) as error:
            self._send(refusal_status(error), {"error": str(error)})
        else:
            self._send(status, result)

    @staticmethod
    def _stop(state, client, payload):
        if not isinstance(payload, dict) or set(payload) - {"wait_s"}:
            raise ValueError("Stop must be a JSON object with only wait_s")
        return state.stop(client, payload.get("wait_s", 0))

    def _contract_post(self, state, route, payload):
        """Serve one contract POST. A refusal of the robot core is a 409 conflict."""
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object")
        if state.mode != "robot":
            # Read-only mode: start_sync and stop_sync read the feedback. Nothing else works.
            if route != "api/command":
                raise PermissionError("Robot control is unavailable in read-only mode")
            return 200, state.command(payload)
        client = self.headers.get("X-MG400-Client")
        if not valid_client(client):
            raise ValueError("A printable ASCII client ID is required")
        try:
            if route == "api/stop":
                return 200, self._stop(state, client, payload)
            if route == "api/command":
                # Key renewals arrive every 150 ms; keep them out of the request log.
                self._quiet = payload.get("action") == "keys"
                command_id, snapshot = state.submit(payload, client)
                return 202, {"command_id": command_id, "state": snapshot}
            if payload:
                raise ValueError("Lease requests take an empty JSON object")
            verb = {"api/heartbeat": state.acquire, "api/renew": state.renew}
            lease = verb.get(route, state.release)(client)
            return 200, {"lease": lease}
        except PermissionError as error:
            raise RuntimeError(str(error)) from error

    def log_message(self, format, *args):
        # Keep request bodies out of the request log.
        if format.startswith("Request timed out"):
            return
        polls = (
            f"GET {self.server.base_path}api/state ",
            f"GET {self.server.base_path}api/describe ",
            f"GET {self.server.base_path}api/events",
            f"POST {self.server.base_path}api/heartbeat ",
            f"POST {self.server.base_path}api/renew ",
        )
        if len(args) > 1 and (
            (isinstance(args[0], str) and args[0].startswith(polls) and str(args[1]) == "200")
            or (getattr(self, "_quiet", False) and str(args[1]) == "202")
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


def start_event_log():
    """Write robot service events to stderr on a helper thread.

    Events are logged under the state lock, so a blocked stderr must not delay a stop.
    Return a function that flushes for at most wait seconds and removes the handler.
    """
    events = queue.SimpleQueue()
    output = logging.StreamHandler(sys.stderr)
    output.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%d/%b/%Y %H:%M:%S")
    )
    listener = logging.handlers.QueueListener(events, output)
    handler = logging.handlers.QueueHandler(events)
    logger = logging.getLogger("mg400")
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    listener.start()

    def stop(wait):
        logger.removeHandler(handler)
        logger.setLevel(level)
        try:
            stopper = threading.Thread(target=listener.stop, daemon=True)
            stopper.start()
            stopper.join(wait)
        except RuntimeError:
            pass

    return stop


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1", help="HTTP address (default: 127.0.0.1)")
    parser.add_argument(
        "--port", type=int, default=ports.PORTS["robot"], help="HTTP port (default: %(default)s)"
    )
    parser.add_argument("--mode", choices=("robot", "read-only"), default="robot")
    parser.add_argument("--base-path", type=normalize_base_path, default="/")
    parser.add_argument(
        "--robot-host", help="Robot address. Default: MG400_ROBOT_HOST, then config/robot.toml"
    )
    args = parser.parse_args(argv)
    bind, port = args.bind, args.port
    if not 1 <= port <= 65535:
        parser.error("--port must be a whole number between 1 and 65535")
    caps = {"max_speed_l": MAX_SPEED_L, "max_acc_l": MAX_ACC_L}
    host = args.robot_host
    if not host:
        host = load_robot_host(ROOT / "config/robot.toml")
    state = RobotPanelState(host, **caps) if args.mode == "robot" else ReadOnlyPanelState(host)
    server = PanelServer((bind, port), state, args.base_path)
    print(
        f"MG400 panel: http://{bind}:{server.server_port}{args.base_path} ({args.mode})",
        flush=True,
    )
    stop_event_log = start_event_log()
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
            stop_event_log(0.2)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
