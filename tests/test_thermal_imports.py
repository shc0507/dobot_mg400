"""Import checks without devices or network access."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_imports_do_not_open_devices_start_threads_or_write_files(tmp_path):
    script = r"""
import builtins
import pathlib
import socket
import sys
import threading
import usb.core


def forbidden(*args, **kwargs):
    raise AssertionError("An import attempted an external side effect")


original_open = builtins.open


def read_only_open(file, mode="r", *args, **kwargs):
    if any(flag in mode for flag in "wax+"):
        forbidden()
    return original_open(file, mode, *args, **kwargs)


builtins.open = read_only_open
pathlib.Path.write_text = forbidden
pathlib.Path.write_bytes = forbidden
socket.socket.bind = forbidden
socket.socket.connect = forbidden
usb.core.find = forbidden
threading.Thread.start = forbidden

import thermal_cam
import thermal_cam.__main__
import thermal_cam.app
import thermal_cam.camera
import thermal_cam.service

# Never read an installed host's region file. An absent file defines no regions.
thermal_cam.service.CameraService.__init__.__kwdefaults__["roi_path"] = sys.argv[1]
application = thermal_cam.app.create_app(auto_connect=False)
assert application.state.camera.status()["state"] == "disconnected"
"""
    subprocess.run(
        [sys.executable, "-B", "-c", script, str(tmp_path / "absent")],
        check=True,
        cwd=ROOT,
        timeout=30,
    )
