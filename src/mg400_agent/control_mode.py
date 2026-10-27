"""Read MG400 mode settings or apply TCP mode through SMB and HTTP."""

import argparse
import http.client
import io
import json
import math
import os
import socket
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .controller_ownership import ControllerOwnership
from .controller_protocol import ControllerCommandError, parse_controller_response
from .robot_config import load_robot_host

CONFIG_PATH = "/settings/function/remoteControl.json"
MODE_PATH = "/settings/function/remoteControl"
ROOT = Path(__file__).resolve().parents[2]


class ControllerTransport:
    def __init__(self, host, timeout=5.0):
        self.host = host
        self.timeout = timeout

    @contextmanager
    def _smb(self):
        try:
            from smb.SMBConnection import SMBConnection
        except ImportError as error:
            raise RuntimeError("Install the SMB dependency: python -m pip install pysmb") from error
        connection = SMBConnection(
            "", "", "mg400mode", "MG400", use_ntlm_v2=True, is_direct_tcp=True
        )
        try:
            if not connection.connect(self.host, 445, timeout=self.timeout):
                raise RuntimeError("The controller rejected the anonymous SMB connection.")
            yield connection
        finally:
            connection.close()

    def read_config(self):
        stream = io.BytesIO()
        with self._smb() as connection:
            connection.retrieveFile("project", CONFIG_PATH, stream, timeout=self.timeout)
        return stream.getvalue()

    def write_config(self, content):
        with self._smb() as connection:
            connection.storeFile("project", CONFIG_PATH, io.BytesIO(content), timeout=self.timeout)

    def http(self, method, path, body=None):
        connection = http.client.HTTPConnection(self.host, 22000, timeout=self.timeout)
        try:
            connection.request(method, path, body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            data = response.read(1024 * 1024 + 1)
            if response.status != 200 or len(data) > 1024 * 1024:
                raise RuntimeError(f"Invalid HTTP response for {path}: {response.status}")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise RuntimeError(f"Expected a JSON object from {path}.")
            return result
        finally:
            connection.close()

    def dashboard_mode(self):
        deadline = time.monotonic() + self.timeout
        with socket.create_connection((self.host, 29999), timeout=self.timeout) as connection:
            connection.sendall(b"RobotMode()")
            response = bytearray()
            while b";" not in response and len(response) < 4096:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("RobotMode() timed out.")
                connection.settimeout(remaining)
                chunk = connection.recv(4096 - len(response))
                if not chunk:
                    break
                response.extend(chunk)
        return parse_controller_response(response, "RobotMode()")


def decode_config(content):
    config = json.loads(content)
    if not isinstance(config, dict) or not isinstance(config.get("mode"), str):
        raise RuntimeError("The mode configuration must contain a string named mode.")
    return config


def require_disabled(status):
    expected = {"controlMode": "disable", "prjState": "stopped"}
    for field, value in expected.items():
        if status.get(field) != value:
            raise RuntimeError(f"Switch refused: {field} must be {value!r}.")
    for field in ("isCollision", "isSafeSuspend"):
        if type(status.get(field)) is not int or status[field] != 0:
            raise RuntimeError(f"Switch refused: {field} must be zero.")
    alarms = status.get("alarms")
    if not isinstance(alarms, list) or not alarms or any(item not in (None, []) for item in alarms):
        raise RuntimeError("Switch refused: alarm status is missing, invalid, or not empty.")


def read_status(transport):
    config = decode_config(transport.read_config())
    return {
        "host": transport.host,
        "configured_mode": config["mode"],
        "remote_config": config,
        "controller_status": transport.http("GET", "/protocol/exchange"),
        "version": transport.http("POST", "/settings/version", b""),
        "tcp_verified": False,
    }


def switch_to_tcp(transport, backup_dir):
    ownership = ControllerOwnership(transport.host)
    ownership.acquire()
    try:
        original = transport.read_config()
        config = decode_config(original)
        if config["mode"] not in ("tp", "tcp"):
            raise RuntimeError("Switch refused: the saved mode must be tp or tcp.")
        require_disabled(transport.http("GET", "/protocol/exchange"))
        if config["mode"] == "tcp":
            try:
                payload = transport.dashboard_mode()
            except ControllerCommandError as error:
                if error.code != -1:
                    raise
                # A previous run can save TCP mode without applying it.
                # This explicit invocation can apply it once after fresh checks.
            else:
                if payload != "4":
                    raise RuntimeError(
                        "TCP verification failed: RobotMode() must return disabled mode 4."
                    )
                require_disabled(transport.http("GET", "/protocol/exchange"))
                return {"configured_mode": "tcp", "tcp_verified": True, "changed": False}
        target = {**config, "mode": "tcp", "name": ""}
        content = json.dumps(target, ensure_ascii=False).encode("utf-8")
        directory = Path(backup_dir).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        fd, backup = tempfile.mkstemp(prefix="remoteControl-", suffix=".json", dir=directory)
        with os.fdopen(fd, "wb") as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        if transport.read_config() != original:
            raise RuntimeError("Switch refused: the saved configuration changed during the check.")
        require_disabled(transport.http("GET", "/protocol/exchange"))
        try:
            transport.write_config(content)
            if transport.read_config() != content:
                raise RuntimeError("The SMB readback does not match the requested configuration.")
            reply = transport.http("POST", MODE_PATH, content)
            if reply.get("status") is not True:
                raise RuntimeError(f"The controller rejected the mode request: {reply!r}")
            if decode_config(transport.read_config()) != target:
                raise RuntimeError("The saved configuration changed after the mode request.")
            require_disabled(transport.http("GET", "/protocol/exchange"))
            if transport.dashboard_mode() != "4":
                raise RuntimeError(
                    "TCP verification failed: RobotMode() must return disabled mode 4."
                )
        except BaseException as error:
            raise RuntimeError(
                f"The controller state may have changed. Backup: {backup}. "
                f"No rollback or write retry was attempted. Cause: {error}"
            ) from error
        return {"configured_mode": "tcp", "tcp_verified": True, "changed": True, "backup": backup}
    finally:
        ownership.release()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "By default, read saved settings and controller state. "
            "configured_mode is the saved value. It does not prove TCP command access."
        )
    )
    parser.add_argument("--host", help="Controller IP address. Defaults to config/robot.toml.")
    parser.add_argument("--timeout", type=float, default=5.0, help="Request timeout in seconds.")
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=ROOT / "work/control-mode",
        help="Save the original configuration here before a write. Default: work/control-mode.",
    )
    parser.add_argument(
        "--switch-to-tcp", action="store_true", help="Write TCP mode and check it without motion."
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 30:
        parser.error("--timeout must be greater than zero and at most 30 seconds.")
    try:
        host = args.host or load_robot_host(ROOT / "config/robot.toml")
        transport = ControllerTransport(host, args.timeout)
        result = (
            switch_to_tcp(transport, args.backup_dir)
            if args.switch_to_tcp
            else read_status(transport)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
