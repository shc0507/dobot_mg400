"""Service adapters and leases. No endpoint is contacted during construction."""

import copy
import os
import threading
import time
import urllib.request

from labkit.client import ServiceClient
from labkit.errors import ServiceError
from mg400.client import RobotClient, RobotClientError

from .recipe import positive

SERVICES = ("robot", "stage", "thermal", "vision", "furnace", "microscope")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class ServiceAdapter:
    def __init__(self, url, token, identity, *, timeout=2):
        self.identity, self.timeout = identity, timeout
        self._lease_seconds = 0
        self._http = ServiceClient(url, token, identity, timeout=timeout)
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

    def stop(self):
        return self._http.stop(wait_s=min(1, self.timeout / 2))

    def release(self):
        return self._http.release()

    def close(self):
        pass


class RobotAdapter(ServiceAdapter):
    def __init__(self, url, token, identity, *, timeout=2):
        super().__init__(url, token, identity, timeout=timeout)
        self._robot = RobotClient(url, token or "", identity, timeout=timeout)

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
            adapter = RobotAdapter if name == "robot" else ServiceAdapter
            result[name] = adapter(
                url, settings.get(prefix + "_TOKEN", ""), identity, timeout=timeout
            )
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
        with self._lock:
            if round_id is None:
                self.stop_round += 1
                round_id = self.stop_round

        def stop(name):
            try:
                result = self.services[name].stop()
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
            if (
                not service.owned(state)
                or not service.healthy(state)
                or state.get("busy") is not False
                or state.get("locked") is not False
                or "fault" not in state
                or state["fault"] is not None
                or ("stop_unconfirmed" in state and state["stop_unconfirmed"] is not False)
                or state.get("instance") != self._instances[name]
            ):
                raise RuntimeError(
                    f"{name} still needs manual recovery or no longer belongs to this run."
                )
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
