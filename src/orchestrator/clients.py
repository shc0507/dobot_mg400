"""Service adapters and leases. No endpoint is contacted during construction."""

import copy
import http.client
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from labkit.client import ServiceClient
from labkit.errors import ServiceError
from mg400.client import RobotClient, RobotClientError

from .recipe import positive

SERVICES = ("robot", "stage", "thermal", "vision", "furnace", "microscope")

# Fixed loopback addresses of the lab services. A nonempty setting overrides one.
SERVICE_URLS = {
    "MG400_ORCH_ROBOT_URL": "http://127.0.0.1:8766/",
    "MG400_ORCH_STAGE_URL": "http://127.0.0.1:8767/",
    "MG400_ORCH_THERMAL_URL": "http://127.0.0.1:8768/",
    "MG400_ORCH_FURNACE_URL": "http://127.0.0.1:8769/",
    "MG400_ORCH_MICROSCOPE_URL": "http://127.0.0.1:8770/",
    "MG400_ORCH_VISION_URL": "http://127.0.0.1:8771/",
}


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
            urllib.request.ProxyHandler({}), _NoRedirect()
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
        """Send one Lab Console operator request with this identity. Never retried.

        With data it is a POST. The runner and the recipes never call this.
        """
        return self._http._call(path, data)

    def events(self, since=0):
        """Read the contract event replay once; the robot service serves the same route."""
        return self._http.events(since)

    def stop(self):
        return self._http.stop(wait_s=min(1, self.timeout / 2))

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


class _GasStopClient(ServiceClient):
    """Bound gas discovery and response bodies without changing other requests.

    The body deadline is checked between bounded read1 calls. Connect, headers,
    framing, and each blocking read retain the configured socket timeout; this
    is not a hard deadline for the complete HTTP exchange.
    """

    _BODY_LIMIT = 256 * 1024

    def _body(self, response):
        deadline = time.monotonic() + self.timeout
        chunks, size = [], 0
        while True:
            if time.monotonic() >= deadline:
                raise ServiceError(0, "Gas-stop response body deadline exceeded.")
            block = response.read1(min(64 * 1024, self._BODY_LIMIT - size + 1))
            if time.monotonic() >= deadline:
                raise ServiceError(0, "Gas-stop response body deadline exceeded.")
            if not block:
                return b"".join(chunks)
            size += len(block)
            if size > self._BODY_LIMIT:
                raise ServiceError(502, "Gas-stop response exceeds 256 KiB.")
            chunks.append(block)

    def _request(self, path, data=None):
        headers = {"Accept": "application/json"}
        encoded = None
        if data is not None:
            encoded = json.dumps(data, allow_nan=False).encode("utf-8")
            headers.update({"Content-Type": "application/json", "X-MG400-Client": self.client_id})
        request = urllib.request.Request(self.base_url + path, data=encoded, headers=headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                payload = self._body(response)
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(self._body(error)).get("error", error.reason)
            except (ValueError, AttributeError, OSError, http.client.HTTPException, ServiceError):
                detail = error.reason
            finally:
                error.close()
            raise ServiceError(error.code, detail) from error
        except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
            raise ServiceError(0, f"Service request failed: {error}") from error
        try:
            result = json.loads(payload)
        except (ValueError, UnicodeError) as error:
            raise ServiceError(502, "The service returned invalid JSON.") from error
        if not isinstance(result, dict):
            raise ServiceError(502, "The service response must be a JSON object.")
        return result


class FurnaceAdapter(ServiceAdapter):
    def __init__(self, url, identity, *, timeout=2):
        super().__init__(url, identity, timeout=timeout)
        self._gas_http = _GasStopClient(url, identity, timeout=timeout)
        self._gas_http._opener = self._http._opener

    def gas_stop(self, channel):
        """Stop one gas channel without acquiring a lease or requesting a full stop."""
        if type(channel) is not int or not 0 <= channel <= 2:
            raise ValueError("Gas channel must be an integer from 0 to 2.")
        return self._gas_http._call("api/flow/gas/stop", {"channel_index": channel})


class RobotAdapter(ServiceAdapter):
    def __init__(self, url, identity, *, timeout=2):
        super().__init__(url, identity, timeout=timeout)
        self._robot = RobotClient(url, identity, timeout=timeout)

    def acquire(self):
        self._robot.connect()
        return self._robot.run("start_sync", timeout=self.timeout)

    def owned(self, state):
        return state.get("controller_id") == self.identity

    def healthy(self, state):
        return state.get("syncing") is True and state.get("feedback_fresh") is True

    def heartbeat(self):
        # RobotClient renews at 0.5 s. Loss is terminal for this run, not a reason
        # to acquire ownership again through start_sync.
        if not self._robot.connected or not self.owned(self.state()):
            raise RuntimeError("Robot connection or control lease was lost.")

    def run(self, action, *, timeout, **params):
        return self._robot.run(action, timeout=timeout, **params)

    def command(self, action, **params):
        return self._robot.command(action, **params)

    def stop(self):
        # The robot's dedicated HTTP transport includes X-MG400-Panel and
        # remains independent of an in-flight WebSocket command.
        return self._robot._http("stop", {"wait_s": min(1, self.timeout / 2)})

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
                        "gripper_busy",
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

    def release(self):
        return self._robot.run("stop_sync", timeout=self.timeout)

    def close(self):
        self._robot.close()


def from_settings(settings=None, *, identity):
    settings = os.environ if settings is None else settings
    timeout = positive(float(settings.get("MG400_ORCH_TIMEOUT_S", "2")), "request timeout")
    result = {}
    for name in SERVICES:
        prefix = f"MG400_ORCH_{name.upper()}"
        url = settings.get(prefix + "_URL")
        if url:
            adapter = {
                "robot": RobotAdapter,
                "thermal": ThermalAdapter,
                "furnace": FurnaceAdapter,
            }.get(name, ServiceAdapter)
            result[name] = adapter(url, identity, timeout=timeout)
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
            except (ServiceError, RobotClientError) as exc:
                if exc.status in {401, 403, 409}:
                    self.acquired.remove(name)  # An explicit refusal acquired nothing.
                raise
            state = service.state()
            if not service.owned(state):
                raise RuntimeError(f"{name} did not grant ownership to this run.")
            self._instances[name] = state.get("instance")
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
            if name == "robot":
                # The kept robot protocol reports these fields instead of the
                # generic contract's connected/fault pair.
                fault_free = (
                    state.get("mode") == "robot"
                    and state.get("robot_error") is False
                    and state.get("stop_unconfirmed") is False
                    and isinstance(state.get("instance"), str)
                    and bool(state["instance"].strip())
                    and state.get("fault") is None
                )
            else:
                fault_free = "fault" in state and state["fault"] is None
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
