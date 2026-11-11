"""Agent access to the lab through the Lab Console. Every function returns a JSON-ready dict.

An agent session is one console page, ``agent-<session>``. The console holds its
leases between calls and releases them after 10 min without a request. Commands go
through the console relay and are sent once. Stops need no control: they go to the
console and, in parallel, straight to the services, so they work without the console.
"""

import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from labkit import ports
from labkit.errors import ServiceError
from mg400.client import RobotClientError, command_complete

from .clients import SERVICE_URLS, SERVICES, RobotAdapter, ServiceAdapter

DEFAULT_URL = ports.url("orch", "lab/")
CAMERA_URL = ports.url("camera")
SESSION = re.compile(r"[A-Za-z0-9_.-]{1,64}")
MOTION = ("robot", "stage", "microscope")
FINAL = {"ok", "failed", "rejected", "stopped"}
POLL_S = 0.2
BEAT_S = 1.0
DEFAULT_TIMEOUT_S = 300.0
STOP_TIMEOUT_S = 2.0
DO_TIMEOUT_S = 1800.0
ABORT_WAIT_S = 30.0
EXIT = {"usage": 2, "invalid": 2, "unknown": 3, "timeout": 3, "interrupted": 130}


class Unreachable(Exception):
    """The console did not answer. Nothing is known about the request."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class Http:
    """JSON over HTTP to the console, without proxies, redirects or retries."""

    def __init__(self, base, page, *, timeout=15):
        self.base, self.page, self.timeout = base.rstrip("/") + "/", page, timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def request(self, path, data=None, *, raw=False):
        """Return (status, body). The body is a dict, or bytes when raw is true."""
        url = path if "://" in path else self.base + path
        headers = {"Accept": "application/json", "X-MG400-Client": self.page}
        encoded = None
        if data is not None:
            encoded = json.dumps(data, allow_nan=False).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=encoded, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                status, payload = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, payload = error.code, error.read()
            error.close()
        except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
            raise Unreachable(f"The console did not answer: {error}") from error
        if raw and status < 300:
            return status, payload
        try:
            body = json.loads(payload) if payload else {}
        except (ValueError, UnicodeError):
            body = {"error": payload[:200].decode("utf-8", "replace")}
        return status, body if isinstance(body, dict) else {"value": body}


def exit_code(result):
    """0 ok, 1 refused or failed, 2 usage, 3 outcome unknown, 130 interrupted."""
    if result.get("ok"):
        return 0
    if result.get("outcome") in {"unknown", "interrupted"}:
        return EXIT[result["outcome"]]
    return EXIT.get((result.get("error") or {}).get("code"), 1)


def code_for(status, message):
    """Map an HTTP status and message to an error code."""
    text = (message or "").lower()
    if status in {400, 404, 405, 415, 422}:
        return "invalid"
    if status == 409:
        if "take control" in text or "acquire the control lease" in text:
            return "not_controlled"
        if "another" in text or "holds" in text or "controls this device" in text:
            return "held_by_other"
        if "locked" in text:
            return "locked"
        if "busy" in text or "in progress" in text or "run is using" in text:
            return "busy"
        return "refused"
    if status == 0:
        return "unreachable"
    return "unknown"


def hint(device, code, state=None):
    """The next command to run, from a small fixed table."""
    state = state if isinstance(state, dict) else {}
    if device == "robot" and state.get("alarms"):
        return "labctl robot clear-alarm"
    if device == "microscope" and state.get("focus_zero_known") is False:
        return "labctl microscope zero"
    return {
        "not_controlled": f"labctl {device} take",
        "held_by_other": "labctl all session",
        "locked": "labctl robot disable; labctl robot enable"
        if device == "robot"
        else f"labctl {device} state",
        "unknown": f"labctl {device} state",
        "timeout": f"labctl {device} state",
        "busy": f"labctl {device} wait",
    }.get(code)


def failure(code, message, *, outcome=None, status=None, device=None, state=None, **extra):
    if outcome is None:
        outcome = {
            "usage": "refused",
            "invalid": "refused",
            "unknown": "unknown",
            "timeout": "unknown",
            "interrupted": "interrupted",
        }.get(code, "refused")
    result = {"ok": False, **({"device": device} if device else {}), **extra, "outcome": outcome}
    result["error"] = {"code": code, "status": status, "message": message}
    next_step = hint(device, code, state) if device else None
    if next_step:
        result["hint"] = next_step
    return result


def _unknown(message):
    return {"code": "unknown", "status": None, "message": message}


def parse_value(text, stdin=None):
    """JSON when it parses, else the string. @FILE reads JSON from a file, @- from stdin."""
    if text.startswith("@"):
        source = (stdin.read() if stdin is not None else "") if text == "@-" else None
        if source is None:
            source = Path(text[1:]).read_text(encoding="utf-8")
        return json.loads(source)
    try:
        return json.loads(text)
    except ValueError:
        return text


def parse_params(items, stdin=None):
    result = {}
    for item in items:
        name, separator, value = item.partition("=")
        if not separator or not name or name in result:
            raise ValueError(f"Expected unique NAME=VALUE parameters, got {item!r}.")
        result[name] = parse_value(value, stdin)
    return result


class Agent:
    def __init__(
        self,
        url=DEFAULT_URL,
        session="main",
        *,
        http=None,
        settings=None,
        direct=None,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        if not isinstance(session, str) or not SESSION.fullmatch(session):
            raise ValueError("A session name is 1 to 64 letters, digits, _, . or -.")
        self.session, self.page = session, "agent-" + session
        self.http = http or Http(url, self.page)
        settings = {} if settings is None else settings
        # Tests pass their own URLs. The host environment cannot change them.
        self.urls = {
            name: settings.get(f"MG400_ORCH_{name.upper()}_URL")
            or SERVICE_URLS[f"MG400_ORCH_{name.upper()}_URL"]
            for name in SERVICES
        }
        self.camera_url = settings.get("MG400_AGENT_CAMERA_URL") or CAMERA_URL
        # Direct stops reach the fixed loopback services. Another console (a demo)
        # has other services, so its stops go only through that console.
        self.direct = direct if direct is not None else url == DEFAULT_URL
        self.clock, self.sleep = clock, sleep

    # Console requests -------------------------------------------------------

    def _get(self, path):
        return self.http.request("api/console/" + path)

    def _post(self, path, data=None):
        return self.http.request("api/console/" + path, {} if data is None else data)

    def beat(self):
        """Stamp this page's sessions; return this page's control entries."""
        status, body = self._post("heartbeat")
        if status != 200:
            raise ServiceError(status, body.get("error", "Heartbeat failed."))
        return [item for item in body.get("control", []) if item.get("mine")]

    def _read(self, device):
        status, body = self._get("state/" + device)
        if status != 200:
            raise ServiceError(status, body.get("error", "State read failed."))
        return body

    def _guard(self, function, device=None):
        """Turn console errors into failure dicts."""
        try:
            return function()
        except Unreachable as error:
            return failure("unreachable", str(error), device=device)
        except ServiceError as error:
            code = code_for(error.status, error.message)
            return failure(code, error.message, status=error.status, device=device)

    # Reads ------------------------------------------------------------------

    def state(self, device):
        def read():
            body = self._read(device)
            control = body.get("control") or {}
            return {
                "ok": True,
                "device": device,
                "state": body.get("state"),
                "holder": control.get("holder"),
                "mine": control.get("mine") is True,
            }

        return self._guard(read, device)

    def all_state(self):
        def read():
            status, body = self._get("state")
            if status != 200:
                raise ServiceError(status, body.get("error", "State read failed."))
            devices = {}
            for name in SERVICES:
                try:
                    devices[name] = self._read(name).get("state")
                except (Unreachable, ServiceError) as error:
                    devices[name] = {"error": str(error)}
            mine = {item.get("device") for item in body.get("control", []) if item.get("mine")}
            return {
                "ok": True,
                "devices": devices,
                "control": body.get("control", []),
                "mine": sorted(mine),
                "run": body.get("run"),
            }

        return self._guard(read)

    def describe(self, device):
        def read():
            status, body = self._get("describe/" + device)
            if status != 200:
                raise ServiceError(status, body.get("error", "Describe failed."))
            return {"ok": True, "device": device, "commands": body.get("commands", {})}

        return self._guard(read, device)

    def events(self, since=0, device=None):
        def read():
            status, body = self._get("events")
            if status != 200:
                raise ServiceError(status, body.get("error", "Event read failed."))
            rows = [
                item
                for item in body.get("events", [])
                if (device is None or item.get("service") == device)
                and (type(item.get("seq")) is int and item["seq"] > since)
            ]
            return {"ok": True, "events": rows, "errors": body.get("errors", {})}

        return self._guard(read, device)

    def session_report(self):
        def read():
            return {"ok": True, "page": self.page, "control": self.beat()}

        return self._guard(read)

    # Control ----------------------------------------------------------------

    def take(self, device):
        def send():
            status, body = self._post(f"control/{device}/take")
            if status != 200:
                raise ServiceError(status, body.get("error", "Take control failed."))
            return {"ok": True, "device": device, "control": body.get("control")}

        return self._guard(send, device)

    def release(self, device):
        def send():
            status, body = self._post(f"control/{device}/release")
            if status != 200:
                raise ServiceError(status, body.get("error", "Release failed."))
            result = body.get("result")
            if result == "unconfirmed":
                return failure("unknown", "The release is unconfirmed.", device=device)
            return {"ok": True, "device": device, "result": result}

        return self._guard(send, device)

    def release_all(self):
        def send():
            status, body = self._post("release-page")
            if status != 200:
                raise ServiceError(status, body.get("error", "Release failed."))
            results = body.get("results", {})
            ok = all(value == "released" for value in results.values())
            if not ok:
                return {
                    **failure("unknown", "A release is unconfirmed."),
                    "results": results,
                }
            return {"ok": True, "results": results}

        return self._guard(send)

    # Commands ---------------------------------------------------------------

    def call(self, device, action, params=None, *, wait=True, timeout=DEFAULT_TIMEOUT_S):
        """Send one described command once; wait for its result unless wait is false."""
        params = {} if params is None else params
        info = {"action": action}
        try:
            control = {item["device"]: item for item in self.beat()}
            holder = control.get(device, {}).get("holder")
            before = self._read(device).get("state") if wait else None
        except Unreachable as error:
            return failure("unreachable", str(error), device=device, **info)
        except ServiceError as error:
            code = code_for(error.status, error.message)
            return failure(code, error.message, status=error.status, device=device, **info)
        try:
            status, body = self._post("command/" + device, {"action": action, "params": params})
        except Unreachable as error:
            return failure("unknown", str(error), device=device, **info)
        if status == 502 or status >= 500:
            message = body.get("error", "Command outcome is unknown.")
            return failure("unknown", message, status=status, device=device, **info)
        if status != 202:
            message = body.get("error", "The command was refused.")
            code = code_for(status, message)
            return failure(code, message, status=status, device=device, **info)
        command_id = body.get("command_id")
        if not wait or (device == "robot" and holder is None):
            # Without this page's robot session, the command has a fresh identity.
            return {
                "ok": True,
                "device": device,
                **info,
                "outcome": "accepted",
                "command_id": command_id,
            }
        try:
            return self._await(device, action, command_id, holder, before, timeout, info)
        except KeyboardInterrupt:
            stopped = self.stop(device)
            return {
                **failure(
                    "interrupted", "Interrupted; the device stop was sent.", device=device, **info
                ),
                "command_id": command_id,
                "stop": stopped,
            }

    def _await(self, device, action, command_id, holder, before, timeout, info):
        before = before if isinstance(before, dict) else {}
        baseline = (before.get("last_command") or {}).get("id")
        instance = before.get("instance")
        deadline = self.clock() + timeout
        beat_at = self.clock() + BEAT_S
        state = None
        while True:
            try:
                state = self._read(device).get("state")
            except (Unreachable, ServiceError):
                state = None
            if isinstance(state, dict):
                verdict = self._verdict(
                    device, action, command_id, holder, baseline, instance, state
                )
                if verdict is not None:
                    return self._finish(verdict, state, command_id, device, info)
            if self.clock() >= deadline:
                return {
                    **failure(
                        "timeout",
                        f"No result within {timeout:g} s.",
                        device=device,
                        state=state,
                        **info,
                    ),
                    "command_id": command_id,
                }
            if self.clock() >= beat_at:
                beat_at = self.clock() + BEAT_S
                try:
                    self.beat()
                except (Unreachable, ServiceError):
                    pass
            self.sleep(POLL_S)

    @staticmethod
    def _verdict(device, action, command_id, holder, baseline, instance, state):
        """Return ok, failed, rejected, stopped, locked or unknown; None while pending."""
        last = state.get("last_command") or {}
        if device == "robot":
            try:
                done = command_complete(
                    state, action=action, client_id=holder, baseline=baseline, instance=instance
                )
            except RobotClientError as error:
                if last.get("action") == action and last.get("client") == holder:
                    if last.get("result") in FINAL - {"ok"}:
                        return last["result"]
                    if "locked" in str(error):
                        return "locked"
                return "unknown"
            return "ok" if done else None
        if command_id is not None and last.get("id") == command_id:
            return last.get("result") if last.get("result") in FINAL else "unknown"
        if command_id is None and last.get("id") not in (None, baseline):
            return last.get("result") if last.get("result") in FINAL else "unknown"
        if last.get("id") not in (None, baseline) and last.get("client") == holder:
            # A newer command of this session replaced it before its result was seen.
            return "unknown"
        return None

    @staticmethod
    def _finish(verdict, state, command_id, device, info):
        last = state.get("last_command") or {}
        message = last.get("message")
        if verdict == "ok":
            return {
                "ok": True,
                "device": device,
                **info,
                "outcome": "ok",
                "command_id": command_id,
                "message": message,
                "last_command": last,
                "state": state,
            }
        if verdict == "unknown":
            code, outcome = "unknown", "unknown"
            message = message or "The command result is unknown."
        elif verdict == "locked":
            code, outcome = "locked", "failed"
            message = "The service is locked; completion is not safe."
        else:
            code, outcome = verdict, verdict
        result = failure(
            code,
            message or f"The command result is {verdict}.",
            outcome=outcome,
            device=device,
            state=state,
            **info,
        )
        result.update(command_id=command_id, last_command=last, state=state)
        return result

    def wait(self, device, command_id=None, *, timeout=DEFAULT_TIMEOUT_S):
        """Wait for a command sent with wait=False; for the robot, until it is idle."""
        info = {"action": "wait"}
        deadline = self.clock() + timeout
        while True:
            try:
                state = self._read(device).get("state")
            except (Unreachable, ServiceError):
                state = None
            if isinstance(state, dict):
                last = state.get("last_command") or {}
                if command_id is None and state.get("busy") is False:
                    return {
                        "ok": True,
                        **info,
                        "outcome": "ok",
                        "last_command": last,
                        "state": state,
                    }
                if command_id is not None and last.get("id") == command_id:
                    verdict = last.get("result") if last.get("result") in FINAL else "unknown"
                    return self._finish(verdict, state, command_id, device, info)
            if self.clock() >= deadline:
                return failure("timeout", f"No result within {timeout:g} s.", device=device, **info)
            self.sleep(POLL_S)

    def stage_ports(self):
        result = self.state("stage")
        if not result.get("ok"):
            return result
        return {"ok": True, "device": "stage", "ports": (result["state"] or {}).get("ports", [])}

    # Composite runs -------------------------------------------------------------

    def do(self, recipe, params=None, *, timeout=DO_TIMEOUT_S):
        """Run 1 catalog recipe with this session's held devices (borrowed control).

        The CLI holds the orchestrator lease during the run: if the CLI dies, the
        lease expires, the run holds and motion stops. A held run is aborted.
        """
        params = {} if params is None else params
        info = {"recipe": recipe}
        try:
            self.beat()
            status, body = self.http.request("api/heartbeat", {})
        except Unreachable as error:
            return failure("unreachable", str(error), **info)
        except ServiceError as error:
            return failure(code_for(error.status, error.message), error.message, **info)
        if status != 200:
            message = body.get("error", "The orchestrator lease was refused.")
            return failure(code_for(status, message), message, status=status, **info)
        done, lost = threading.Event(), []

        def renew():
            while not done.wait(BEAT_S):
                try:
                    status, body = self.http.request("api/renew", {})
                    self.beat()
                except (Unreachable, ServiceError) as error:
                    lost.append(str(error))
                    return
                if status != 200:
                    lost.append(body.get("error", "The orchestrator lease was lost."))
                    return

        worker = threading.Thread(target=renew, name="labctl-renew", daemon=True)
        worker.start()
        run_id = None
        try:
            run_id, result = self._drive(recipe, params, timeout, info, lost)
        except KeyboardInterrupt:
            if run_id is None:
                run_id = self._current_run()
            if run_id is not None:
                self._abort(run_id)
            result = failure("interrupted", "Interrupted; the run abort was sent.", **info)
        finally:
            done.set()
            worker.join(BEAT_S * 3)
            try:
                self.http.request("api/release", {})
            except Unreachable:
                pass
        if run_id is not None:
            result.update(run_id=run_id, steps=self._steps(run_id))
        try:
            result["devices"] = [item["device"] for item in self.beat()]
        except (Unreachable, ServiceError):
            pass
        return result

    def _current_run(self):
        try:
            status, body = self.http.request("api/state")
        except Unreachable:
            return None
        active = body.get("active_run") if status == 200 else None
        return active.get("run_id") if isinstance(active, dict) else None

    def _abort(self, run_id):
        try:
            self.http.request(f"api/runs/{run_id}/abort", {})
        except Unreachable:
            pass

    def _drive(self, recipe, params, timeout, info, lost):
        status, body = self.http.request(
            "api/runs", {"recipe": recipe, "params": params, "borrow": True}
        )
        if status != 202:
            message = body.get("error", "The run was refused.")
            result = failure(code_for(status, message), message, status=status, **info)
            if message.startswith("Take control of these devices first:"):
                names = message.split(":", 1)[1].strip(" .").split(", ")
                result["hint"] = "; ".join(f"labctl {name} take" for name in names)
            return None, result
        command_id = body.get("command_id")
        run_id, aborted = None, False
        deadline = self.clock() + timeout
        while True:
            try:
                status, state = self.http.request("api/state")
            except Unreachable:
                status, state = 0, {}
            if status == 200:
                last = state.get("last_command") or {}
                active = state.get("active_run") or {}
                if run_id is None and last.get("id") == command_id:
                    if last.get("result") != "ok":
                        message = last.get("message") or "The run did not start."
                        return None, failure("failed", message, outcome="failed", **info)
                    run_id = active.get("run_id")
                if run_id is not None and active.get("run_id") == run_id:
                    if active.get("status") == "held" and not aborted:
                        # No operator prompt for agents: a held run is aborted.
                        self._abort(run_id)
                        aborted = True
                    if active.get("closed") and active.get("status") in {"completed", "aborted"}:
                        return run_id, self._outcome(active, info)
            if lost and not aborted:
                message = "The orchestrator lease was lost; the run holds: " + lost[0]
                return run_id, failure("unknown", message, **info)
            if self.clock() >= deadline:
                if run_id is not None and not aborted:
                    self._abort(run_id)
                    aborted = True
                    deadline = self.clock() + ABORT_WAIT_S
                    continue
                return run_id, failure("timeout", f"No result within {timeout:g} s.", **info)
            self.sleep(POLL_S)

    @staticmethod
    def _outcome(active, info):
        if active.get("status") == "completed":
            return {"ok": True, **info, "outcome": "ok", "status": "completed"}
        message = active.get("detail") or "The run was aborted."
        result = failure("failed", str(message), outcome="failed", **info)
        result["status"] = "aborted"
        return result

    def _steps(self, run_id):
        try:
            status, body = self.http.request(f"api/runs/{run_id}")
        except Unreachable:
            return None
        if status != 200:
            return None
        steps = []
        for step in body.get("steps", []):
            try:
                value = json.loads(step["result_json"]) if step.get("result_json") else None
            except (TypeError, ValueError):
                value = step.get("result_json")
            steps.append(
                {
                    "skill": step.get("skill"),
                    "status": step.get("status"),
                    "result": value,
                    "error": step.get("error"),
                }
            )
        return steps

    def chip_locate(self, slot):
        """Run the slot-state recipe for 1 slot and print the slot result."""
        result = self.do("slot-state", {"slot": slot})
        if not result.get("ok"):
            return result
        steps = result.get("steps") or [{}]
        value = steps[-1].get("result") if isinstance(steps[-1], dict) else None
        if not isinstance(value, dict):
            return failure("unknown", "The slot result is missing.", run_id=result.get("run_id"))
        return {
            "ok": True,
            "slot": slot,
            "state": value.get("state"),
            "center_xy_mm": value.get("center_xy_mm"),
            "angle_deg": value.get("angle_deg"),
            "reason": value.get("reason"),
            "run_id": result.get("run_id"),
        }

    # Stops --------------------------------------------------------------------

    def _direct_stop(self, device):
        """Send api/stop straight to the service once. Return a stop result string."""
        identity = f"{self.page}-stop-{uuid.uuid4().hex[:12]}"
        kind = RobotAdapter if device == "robot" else ServiceAdapter
        adapter = None
        try:
            adapter = kind(self.urls[device], identity, timeout=STOP_TIMEOUT_S)
            value = adapter.stop()
        except Exception:
            return "unreachable"
        finally:
            if adapter is not None:
                try:
                    adapter.close()
                except Exception:
                    pass
        stop = value.get("stop") if isinstance(value, dict) else None
        return stop if stop in {"confirmed", "pending", "unconfirmed"} else "unconfirmed"

    def _console_stop(self, device):
        try:
            status, body = self._post("stop/" + device)
        except Unreachable:
            return "unreachable"
        if status == 409:
            # The console has no such service (not configured, or no demo of it).
            return "not-configured"
        outcome = body.get("outcome") if status == 200 else None
        return outcome if outcome in {"confirmed", "pending", "unconfirmed"} else "unconfirmed"

    def _dashboard(self, kind):
        data = {"confirmed": True} if kind in {"heat", "gas"} else {}
        try:
            status, body = self.http.request("api/dashboard/" + kind, data)
        except Unreachable:
            return {"result": "unreachable"}
        if status == 202:
            return {"result": "accepted", "operation_id": body.get("operation_id")}
        return {"result": "refused", "status": status, "error": body.get("error")}

    @staticmethod
    def _parallel(tasks):
        """Run each task once in its own thread; return name -> result."""
        results = {}

        def run(name, function):
            try:
                results[name] = function()
            except Exception as error:
                results[name] = {"error": str(error)}

        workers = [
            threading.Thread(target=run, args=item, daemon=True, name="labctl-" + item[0])
            for item in tasks.items()
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(STOP_TIMEOUT_S * 4)
        return results

    @staticmethod
    def _best(*values):
        order = ["confirmed", "pending", "unconfirmed", "unreachable", "not-configured"]
        values = [value for value in values if value in order]
        return min(values, key=order.index) if values else "unconfirmed"

    def stop(self, device):
        """Stop one device: console stop and direct service stop in parallel, sent once."""
        if device == "furnace":
            return self.furnace_stop()
        tasks = {"console": lambda: self._console_stop(device)}
        if self.direct:
            tasks["direct"] = lambda: self._direct_stop(device)
        sent = self._parallel(tasks)
        result = self._best(sent.get("console"), sent.get("direct"))
        report = {"ok": result == "confirmed", "kind": "stop", "results": {device: result}}
        report["paths"] = sent
        if not report["ok"]:
            report["outcome"] = "unknown"
            report["error"] = _unknown("Stop is " + result + ".")
            report["hint"] = f"labctl {device} state"
        return report

    def stop_all(self):
        """STOP ALL MOTION: the dashboard stop (holds a run) and direct stops in parallel."""
        tasks = {"dashboard": lambda: self._dashboard("motion")}
        for device in MOTION:
            tasks["console:" + device] = lambda device=device: self._console_stop(device)
            if self.direct:
                tasks[device] = lambda device=device: self._direct_stop(device)
        sent = self._parallel(tasks)
        results = {
            device: self._best(sent.get("console:" + device), sent.get(device)) for device in MOTION
        }
        ok = all(value in {"confirmed", "not-configured"} for value in results.values())
        report = {"ok": ok, "kind": "motion", "results": results, "dashboard": sent["dashboard"]}
        if not ok:
            report["outcome"] = "unknown"
            report["error"] = {
                "code": "unknown",
                "status": None,
                "message": "A stop is unconfirmed.",
            }
            report["hint"] = "labctl all state"
        return report

    def furnace_stop(self):
        """Heat and gas off: the dashboard heat stop and the direct furnace stop."""
        tasks = {
            "dashboard": lambda: self._dashboard("heat"),
            "console": lambda: self._console_stop("furnace"),
        }
        if self.direct:
            tasks["direct"] = lambda: self._direct_stop("furnace")
        sent = self._parallel(tasks)
        result = self._best(sent.get("console"), sent.get("direct"))
        ok = result == "confirmed"
        report = {
            "ok": ok,
            "kind": "heat",
            "results": {"furnace": result},
            "dashboard": sent["dashboard"],
        }
        if not ok:
            report["outcome"] = "unknown"
            report["error"] = _unknown("Stop is unconfirmed.")
            report["hint"] = "labctl furnace state"
        return report

    def gas_off(self, channel=None):
        """Stop 1 gas channel, or all 3: console route and direct route, in parallel."""
        channels = [channel] if channel is not None else [0, 1, 2]

        def console(index):
            try:
                status, body = self.http.request(
                    "api/console/ws/process/gas-stop", {"channel_index": index}
                )
            except Unreachable:
                return "unreachable"
            return body.get("outcome", "unconfirmed") if status == 200 else "unconfirmed"

        def direct(index):
            identity = f"{self.page}-gas-{uuid.uuid4().hex[:12]}"
            try:
                value = ServiceAdapter(self.urls["furnace"], identity, timeout=STOP_TIMEOUT_S)
                value = value.stop(gas_channel=index)
            except Exception:
                return "unreachable"
            return "sent" if isinstance(value, dict) else "unconfirmed"

        tasks = {}
        for index in channels:
            tasks[f"console:{index}"] = lambda index=index: console(index)
            if self.direct:
                tasks[f"direct:{index}"] = lambda index=index: direct(index)
        sent = self._parallel(tasks)
        results = {
            str(index): "confirmed"
            if sent[f"console:{index}"] == "confirmed"
            else sent.get(f"direct:{index}", sent[f"console:{index}"])
            for index in channels
        }
        ok = all(value == "confirmed" for value in results.values())
        report = {"ok": ok, "kind": "gas", "results": results, "paths": sent}
        if not ok:
            report["outcome"] = "unknown"
            report["error"] = {
                "code": "unknown",
                "status": None,
                "message": "A gas stop is unconfirmed.",
            }
            report["hint"] = "labctl furnace state"
        return report

    # Images -------------------------------------------------------------------

    @staticmethod
    def _out(path, name):
        if path is not None:
            return Path(path)
        directory = Path("/tmp/labctl")
        directory.mkdir(mode=0o755, exist_ok=True)
        return directory / name

    def _save(self, url, out, device, name):
        try:
            status, payload = self.http.request(url, raw=True)
        except Unreachable as error:
            return failure("unreachable", str(error), device=device)
        if status == 204 or not isinstance(payload, bytes) or not payload:
            return failure("failed", "No frame is available.", status=status, device=device)
        if status != 200:
            message = payload.get("error") if isinstance(payload, dict) else None
            return failure(code_for(status, message), message or "Read failed.", status=status)
        path = self._out(out, f"{self.session}-{name}")
        path.write_bytes(payload)
        os.chmod(path, 0o644)
        return {"ok": True, "device": device, "path": str(path), "bytes": len(payload)}

    def camera_still(self, view, out=None):
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", view):
            return failure("usage", "A camera view is 1 to 32 of a-z, 0-9, _ or -.")
        url = urllib.parse.urljoin(self.camera_url, view + ".jpg")
        return self._save(url, out, "camera", f"{view}-{int(time.time())}.jpg")

    def microscope_frame(self, camera, png=False, out=None):
        name = "still.png" if png else "frame.jpg"
        url = urllib.parse.urljoin(self.urls["microscope"], f"api/camera/{camera}/{name}")
        suffix = ".png" if png else ".jpg"
        return self._save(url, out, "microscope", f"{camera}-{int(time.time())}{suffix}")
