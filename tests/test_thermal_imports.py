"""Import and browser authentication checks without devices or network access."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_imports_do_not_open_devices_start_threads_or_write_files():
    script = r"""
import builtins
import pathlib
import socket
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

application = thermal_cam.app.create_app(auto_connect=False)
assert application.state.camera.status()["state"] == "disconnected"
"""
    subprocess.run([sys.executable, "-B", "-c", script], check=True, cwd=ROOT, timeout=30)


def test_dashboard_adds_bearer_only_to_headers_and_keeps_request_options():
    script = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const source = fs.readFileSync("src/thermal_cam/static/app.js", "utf8");
const start = source.indexOf("async function request(");
const end = source.indexOf("function connectionUI(", start);
assert(start >= 0 && end > start);
const input = {value: ""};
const calls = [];
const fetch = async (path, options) => {
  calls.push({path, options});
  return {ok: true, json: async () => ({ok: true})};
};
const request = new Function("fetch", "$", "Headers", "AbortSignal",
  source.slice(start, end) + "return request;")(
    fetch, (id) => {assert.equal(id, "accessToken"); return input;}, Headers, AbortSignal);
(async () => {
  await request("/api/status");
  assert(!calls[0].options.headers.has("Authorization"));
  input.value = "thermal-test-token-length-24";
  await request("/api/gain", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({gain: "high"})});
  const call = calls[1];
  assert.equal(call.path, "/api/gain");
  assert.equal(call.options.headers.get("Authorization"), `Bearer ${input.value}`);
  assert.equal(call.options.headers.get("Content-Type"), "application/json");
  assert.equal(call.options.method, "POST");
  assert.deepEqual(JSON.parse(call.options.body), {gain: "high"});
  input.value = "";
  await request("/api/status");
  assert(!calls[2].options.headers.has("Authorization"));
})().catch((error) => {console.error(error); process.exitCode = 1;});
"""
    subprocess.run(["node", "-e", script], check=True, cwd=ROOT, timeout=30)
