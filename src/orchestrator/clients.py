"""Service adapters and leases. No endpoint is contacted during construction."""

import copy
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from labkit import ports
from labkit.client import _HTTPS, ServiceClient
from labkit.errors import ServiceError

from .recipe import positive

SERVICES = ("robot", "stage", "thermal", "vision", "furnace", "microscope")

# Seconds for one HTTP request to a lab service.
TIMEOUT_S = 2
# Fixed loopback addresses of the lab services. The host environment cannot change them.
SERVICE_URLS = {f"MG400_ORCH_{name.upper()}_URL": ports.url(name) for name in SERVICES}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class ServiceAdapter:
    _client_type = ServiceClient

    def __init__(self, url, identity, *, timeout=2):
        self.identity, self.timeout = identity, timeout
        self._lease_seconds = 0
        self._http = self._client_type(url, identity, timeout=timeout)
        # Instrument endpoints are direct. In particular, generated rehearsal
        # URLs must never travel through an ambient HTTP_PROXY.
        self._http._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(), _HTTPS
        )

    def describe(self):
        result = self._http.describe()
        self._lease_seconds = positive(result.get("lease_seconds"), "lease duration")
        return result

    def acquire(self):
        return self._http.heartbeat()

    def heartbeat(self):
        result = self._http.renew()
        if not self.owned(result):
            raise RuntimeError("Service lease was lost.")

    def state(self):
        return self._http.state()

    def owned(self, state):
        lease = state.get("lease")
        return (
            isinstance(lease, dict)
            and lease.get("holder") == self.identity
            and type(lease.get("remaining_s")) in {int, float}
            and 0 < lease["remaining_s"] <= self._lease_seconds
        )

    def healthy(self, state):
        return state.get("connected") is True

    def command(self, action, **params):
        return self._http.command(action, **params)

    def request(self, path, data=None):
        """Send one request with this identity (a read without data). Never retried."""
        calibration_timeouts = {
            "api/turret-calibration/begin": 15,
            "api/turret-calibration/record": 15,
            "api/turret-calibration/focus_move": 15,
            "api/turret-calibration/measure_response": 55,
            "api/turret-calibration/return_clearance": 55,
        }
        if path in calibration_timeouts:
            # Camera feedback can outlast a normal state read. Give this request
            # its own transport so Stop and lease renewal keep their deadlines.
            capture = self._client_type(
                self._http.base_url, self.identity, timeout=calibration_timeouts[path]
            )
            capture._opener = self._http._opener
            capture._version_checked = self._http._version_checked
            return capture._call(path, data)
        return self._http._call(path, data)

    def events(self, since=0, *, instance=None):
        """Read the contract event replay once; the robot service serves the same route."""
        return self._http.events(since, instance=instance)

    def stop(self, **params):
        """Stop the device, or 1 part of it with service stop params (furnace gas_channel)."""
        return self._http.stop(wait_s=min(1, self.timeout / 2), **params)

    def recovery_stop(self, expected_instance):
        """Reconcile only after old work drains; generic services need a fresh stop."""
        return self.stop()

    def release(self):
        return self._http.release()

    def close(self):
        pass


class _ThermalClient(ServiceClient):
    def roi(self, name):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ValueError("Region names must contain 1 to 64 ASCII letters, digits, _ or -.")
        return self._call("api/roi?" + urllib.parse.urlencode({"name": name}))


class ThermalAdapter(ServiceAdapter):
    _client_type = _ThermalClient

    def roi(self, name):
        """Read one explicit named region without a frame or coordinate fallback."""
        return self._http.roi(name)


STOP_ACTIONS = frozenset({"stop", "disable", "stop_sync"})
RESULTS = frozenset({"ok", "failed", "rejected", "stopped"})
POLL_S = 0.05


class CommandFailed(RuntimeError):
    """A robot command did not complete, or its outcome is unknown."""


def robot_verdict(state, *, action, command_id, instance, baseline=None):
    """Judge one accepted robot command from a state read.

    Return ok, failed, rejected, stopped, locked or unknown. Return None while the
    command is pending. A restart, or a stop that replaced the command, is unknown.
    baseline is the id of the last command before this one was sent.
    """
    if state.get("instance") != instance:
        return "unknown"
    last = state.get("last_command")
    last = last if isinstance(last, dict) else {}
    stop = action in STOP_ACTIONS
    if last.get("id") != command_id:
        replaced = (
            last.get("id") not in (None, baseline)
            and last.get("action") in STOP_ACTIONS
            and last.get("result") != "rejected"
            and not stop
        )
        return "unknown" if replaced else None
    result = last.get("result")
    if result not in RESULTS:
        return "unknown"
    if result == "stopped" and stop:
        result = "ok"
    if result != "ok":
        return result
    if state.get("busy") is not False:
        return None
    if state.get("stop_unconfirmed") or (state.get("locked") and not stop):
        return "locked"
    return "ok"


class RobotAdapter(ServiceAdapter):
    """The robot over instrument contract v1. Commands are accepted, then polled."""

    def __init__(self, url, identity, *, timeout=2, tool_file=None):
        super().__init__(url, identity, timeout=timeout)
        # A rehearsal robot names its own synthetic tool. None selects the host tool.
        self.tool_file = tool_file

    def acquire(self):
        """Take the lease, which also connects the feedback. Wait until it is fresh."""
        result = self._http.heartbeat()
        deadline = time.monotonic() + self.timeout
        while not self.state().get("connected"):
            if time.monotonic() >= deadline:
                raise TimeoutError("Robot feedback did not connect.")
            time.sleep(POLL_S)
        return result

    def run(self, action, *, timeout, **params):
        """Send one command once, then wait for its result and an idle robot."""
        before = self.state()
        instance = before.get("instance")
        baseline = (before.get("last_command") or {}).get("id")
        reply = self._http.command(action, **params)
        state = reply.get("state") if isinstance(reply.get("state"), dict) else before
        return self._settle(
            state, action, reply.get("command_id"), instance, baseline, time.monotonic() + timeout
        )

    def _settle(self, state, action, command_id, instance, baseline, deadline):
        """Read the state until robot_verdict ends the command. Return the final state."""
        while True:
            verdict = robot_verdict(
                state,
                action=action,
                command_id=command_id,
                instance=instance,
                baseline=baseline,
            )
            if verdict == "ok":
                return state
            if verdict is not None:
                last = state.get("last_command") or {}
                raise CommandFailed(last.get("message") or f"The robot command is {verdict}.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for the robot command.")
            time.sleep(min(POLL_S, remaining))
            state = self.state()

    def release(self):
        """Release the lease and wait for the stop that follows, as the lease stop_sync does."""
        before = self.state()
        instance = before.get("instance")
        baseline = (before.get("last_command") or {}).get("id")
        self._http.release()
        deadline = time.monotonic() + self.timeout
        while True:
            state = self.state()
            last = state.get("last_command") or {}
            if (
                last.get("id") != baseline
                and last.get("action") == "stop_sync"
                and last.get("client") == self.identity
            ):
                return self._settle(state, "stop_sync", last["id"], instance, baseline, deadline)
            if (state.get("lease") or {}).get("holder") != self.identity:
                return state
            if time.monotonic() >= deadline:
                raise TimeoutError("Timed out waiting for the robot release.")
            time.sleep(POLL_S)

    def keys(self, seq, held):
        return self._http.command("keys", seq=seq, held=list(held))

    def recovery_stop(self, expected_instance):
        """Confirm explicit manual disable after old work drains, or send Stop.

        Normal cancellation must always use stop(). This recovery-only proof
        avoids relocking a robot whose operator already verified disable/rest.
        It never disables, unlocks, or reacquires control automatically.
        """
        try:
            state = self.state()
            command = state.get("last_command")
            if (
                isinstance(expected_instance, str)
                and expected_instance.strip()
                and state.get("instance") == expected_instance
                and state.get("mode") == "robot"
                and self.owned(state)
                and self.healthy(state)
                and type(state.get("robot_mode")) is int
                and state["robot_mode"] == 4
                and state.get("enabled") is False
                and all(
                    state.get(field) is False
                    for field in (
                        "busy",
                        "locked",
                        "robot_error",
                        "stop_unconfirmed",
                        "keyboard_enabled",
                        "keyboard_arming",
                    )
                )
                and "keyboard_active_key" in state
                and state["keyboard_active_key"] is None
                and state.get("fault") is None
                and isinstance(command, dict)
                and command.get("action") == "disable"
                and command.get("result") == "ok"
                and all(
                    isinstance(command.get(field), str) and command[field].strip()
                    for field in ("id", "client")
                )
                and type(command.get("finished_at")) in {int, float}
                and command["finished_at"] > 0
                and math.isfinite(command["finished_at"])
            ):
                return {"stop": "confirmed", "state": state, "recovery": "verified-disabled-rest"}
        except Exception:
            # Read failures and malformed proof still require an independent stop.
            pass
        return self.stop()


class Borrowed:
    """A console-held adapter lent to one run. The console keeps the lease.

    acquire and heartbeat only check ownership; release does nothing; close
    returns the device to the console. Every other call goes to the adapter.
    """

    def __init__(self, adapter, on_close):
        self._adapter, self._on_close = adapter, on_close
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._adapter, name)

    def acquire(self):
        if not self._adapter.owned(self._adapter.state()):
            raise RuntimeError("The lent device is no longer held by the console.")

    def heartbeat(self):
        if not self._adapter.owned(self._adapter.state()):
            raise RuntimeError("The lent device is no longer held by the console.")

    def release(self):
        return None

    def close(self):
        if not self._closed:
            self._closed = True
            self._on_close()


def from_settings(settings=None, *, identity):
    """Return adapters for the fixed loopback services, or for the URLs in settings."""
    settings = SERVICE_URLS if settings is None else settings
    result = {}
    for name in SERVICES:
        prefix = f"MG400_ORCH_{name.upper()}"
        url = settings.get(prefix + "_URL")
        if url:
            adapter = {
                "robot": RobotAdapter,
                "thermal": ThermalAdapter,
            }.get(name, ServiceAdapter)
            result[name] = adapter(url, identity, timeout=TIMEOUT_S)
    return result


class Clients:
    def __init__(self, services):
        self.services = dict(services)
        self.acquired = []
        self.error = None
        self._renew_stop = threading.Event()
        self._renewers = []
        self._stoppers = {}
        self._stop_rounds = {}
        self._recovery_rounds = set()
        self.stop_round = 0
        self.stop_results = {}
        self._instances = {}
        # The last robot command that this run accepted, at acquisition or resume.
        self._robot_baseline = None
        self._lock = threading.RLock()

    @property
    def lifecycle_lock(self):
        """Serialize authority registration with terminal lease-failure latching."""
        return self._lock

    def _failed(self, error):
        with self._lock:
            self.error = self.error or str(error)

    def acquire(self, required, *, cancelled):
        missing = set(required) - set(self.services)
        if missing:
            raise ValueError(f"Services are not configured: {', '.join(sorted(missing))}")
        self.services = {name: self.services[name] for name in required}
        # Check all contracts before the first lease is acquired.
        for service in self.services.values():
            if cancelled.is_set():
                raise RuntimeError("The run was aborted before acquisition.")
            service.describe()
        for name, service in self.services.items():
            if cancelled.is_set():
                raise RuntimeError("The run was aborted during acquisition.")
            self.acquired.append(name)  # An ambiguous acquisition also needs cleanup.
            try:
                service.acquire()
            except ServiceError as exc:
                if exc.status in {401, 403, 409}:
                    self.acquired.remove(name)  # An explicit refusal acquired nothing.
                raise
            state = service.state()
            if not service.owned(state):
                raise RuntimeError(f"{name} did not grant ownership to this run.")
            self._instances[name] = state.get("instance")
            if name == "robot":
                self.accept({name: state})
            worker = threading.Thread(
                target=self._renew, args=(service,), daemon=True, name=f"orch-lease-{name}"
            )
            worker.start()
            self._renewers.append(worker)

    def _renew(self, service):
        while not self._renew_stop.wait(0.5):
            try:
                service.heartbeat()
            except Exception as exc:
                self._failed(exc)
                return

    def stop_all(self, round_id=None):
        return self._stop_all(round_id)

    def recovery_stop_all(self):
        """Start a recovery round only after the caller drains all old work."""
        return self._stop_all(recovery=True)

    def _stop_all(self, round_id=None, *, recovery=False):
        with self._lock:
            if round_id is None:
                self.stop_round += 1
                round_id = self.stop_round
                if recovery:
                    self._recovery_rounds.add(round_id)
            recovery = round_id in self._recovery_rounds

        def stop(name):
            try:
                service = self.services[name]
                confirm = getattr(service, "recovery_stop", None)
                if recovery and not self.error and callable(confirm):
                    result = confirm(self._instances.get(name))
                else:
                    result = service.stop()
                if not isinstance(result, dict) or result.get("stop") not in {
                    "confirmed",
                    "pending",
                    "unconfirmed",
                }:
                    raise ValueError("The service returned an invalid stop result.")
            except Exception as exc:
                result = {"stop": "unconfirmed", "error": str(exc)}
            with self._lock:
                self.stop_results[name] = result

        with self._lock:
            for name in self.acquired:
                previous = self._stoppers.get(name)
                if (
                    self._stop_rounds.get(name) == round_id
                    or previous is not None
                    and previous.is_alive()
                ):
                    continue
                self.stop_results.pop(name, None)
                try:
                    worker = threading.Thread(
                        target=stop, args=(name,), daemon=True, name=f"orch-stop-{name}"
                    )
                    worker.start()
                except Exception as exc:
                    self.stop_results[name] = {"stop": "unconfirmed", "error": str(exc)}
                else:
                    self._stoppers[name] = worker
                self._stop_rounds[name] = round_id
            workers = list(self._stoppers.values())
        deadline = (
            time.monotonic()
            + max((self.services[name].timeout for name in self.acquired), default=0)
            + 0.5
        )
        for worker in workers:
            worker.join(max(0, deadline - time.monotonic()))
        return round_id

    def stop_snapshot(self):
        with self._lock:
            return copy.deepcopy(self.stop_results)

    @property
    def stops_drained(self):
        with self._lock:
            return not any(worker.is_alive() for worker in self._stoppers.values())

    def round_done(self, round_id):
        with self._lock:
            return self.stops_drained and all(
                self._stop_rounds.get(name) == round_id for name in self.acquired
            )

    @property
    def confirmed(self):
        with self._lock:
            return self.round_done(self.stop_round) and all(
                self.stop_results.get(name, {}).get("stop") == "confirmed" for name in self.acquired
            )

    def accept(self, states):
        """Accept the robot's current last command, so that only later stops count."""
        state = states.get("robot")
        if isinstance(state, dict):
            last = state.get("last_command")
            self._robot_baseline = last.get("id") if isinstance(last, dict) else None

    def foreign_stop(self):
        """Return the robot stop that another client sent after acceptance, or None.

        STOP ALL, a console Stop arm or labctl robot stop confirms and unlocks an
        idle robot, while this run keeps the lease. The run must hold at that stop.
        """
        if "robot" not in self.acquired:
            return None
        service = self.services["robot"]
        last = service.state().get("last_command")
        if (
            isinstance(last, dict)
            and last.get("action") in STOP_ACTIONS
            and last.get("result") != "rejected"
            and last.get("client") != getattr(service, "identity", None)
            and last.get("id") != self._robot_baseline
        ):
            return last
        return None

    def reconcile(self):
        if self.error:
            raise RuntimeError(f"Lease failure cannot be resumed: {self.error}")
        if not self.confirmed:
            raise RuntimeError("Every service must have a confirmed stop.")
        states = {}
        for name in self.acquired:
            service = self.services[name]
            state = service.state()
            recovery_error = f"{name} still needs manual recovery or no longer belongs to this run."
            if not isinstance(state, dict):
                raise RuntimeError(recovery_error)
            fault_free = "fault" in state and state["fault"] is None
            if name == "robot":
                fault_free = (
                    fault_free
                    and state.get("mode") == "robot"
                    and state.get("robot_error") is False
                    and state.get("stop_unconfirmed") is False
                    and isinstance(state.get("instance"), str)
                    and bool(state["instance"].strip())
                )
            if (
                not service.owned(state)
                or not service.healthy(state)
                or state.get("busy") is not False
                or state.get("locked") is not False
                or not fault_free
                or ("stop_unconfirmed" in state and state["stop_unconfirmed"] is not False)
                or state.get("instance") != self._instances[name]
            ):
                raise RuntimeError(recovery_error)
            states[name] = state
        return states

    def release(self):
        self._renew_stop.set()
        for worker in self._renewers:
            worker.join(0.05)
        if any(worker.is_alive() for worker in self._renewers):
            return False
        errors = {}
        for name in self.acquired:
            try:
                self.services[name].release()
            except Exception as exc:
                errors[name] = str(exc)
        for name, service in self.services.items():
            try:
                service.close()
            except Exception as exc:
                errors[f"close:{name}"] = str(exc)
        return errors
