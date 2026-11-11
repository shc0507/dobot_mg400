"""Lease-controlled recipe service. Construction never starts devices or workers."""

import copy
import json
import math
import os
import re
import stat
import tempfile
import threading
import time
import tomllib
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path

from flask import Flask, jsonify, request
from flask.json.provider import DefaultJSONProvider
from werkzeug.exceptions import HTTPException

from labkit.describe import command, describe
from labkit.errors import ServiceError
from labkit.events import EventLog
from labkit.flask import contract_blueprint
from labkit.lease import Lease, validate_client

from .clients import Clients, from_settings
from .recipe import Recipe, validate
from .rehearsal import DEMO_SERVICES
from .runner import Runner
from .skills import default_registry
from .skills.crucible import CRUCIBLE_SETTINGS
from .skills.robot import TRANSFER_SETTINGS
from .skills.vision import DRIFT_SETTINGS
from .store import DEFAULT_RUNS, RunStore
from .zones import CONFIG_SETTING, STATE_SETTING, Zones, prepare_zones

CATALOG_DEFAULT = "/opt/mg400/recipes"
DEMO_MARKER = "MG400_ORCH_DEMO_PROCESS"
DEMO_ENV = (
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "COLORTERM",
        "TZ",
        CONFIG_SETTING,
        DEMO_MARKER,
    }
    | TRANSFER_SETTINGS
    | CRUCIBLE_SETTINGS
    | DRIFT_SETTINGS
)


def demo_environment(environ):
    """Build the entire child environment, never modify a running process."""
    return {**{key: value for key, value in environ.items() if key in DEMO_ENV}, DEMO_MARKER: "1"}


def demo_isolated(environ):
    return environ.get(DEMO_MARKER) == "1" and not set(environ) - DEMO_ENV


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field.")
        result[key] = value
    return result


class StrictJSON(DefaultJSONProvider):
    def loads(self, value, **kwargs):
        def constant(_):
            raise ValueError("JSON numbers must be finite.")

        try:
            result = json.loads(value, object_pairs_hook=_unique, parse_constant=constant)
            stack, count = [(result, 0)], 0
            while stack:
                item, depth = stack.pop()
                count += 1
                if depth > 20 or count > 4096:
                    raise ValueError("JSON exceeds the nesting or item limit.")
                if isinstance(item, dict):
                    stack.extend((child, depth + 1) for child in item.values())
                elif isinstance(item, list):
                    stack.extend((child, depth + 1) for child in item)
                elif type(item) is float and not math.isfinite(item):
                    raise ValueError("JSON numbers must be finite.")
                elif type(item) is int and not -(2**63) <= item < 2**63:
                    raise ValueError("JSON integers must fit in 64 bits.")
            return result
        except RecursionError:
            raise ValueError("JSON exceeds the nesting limit.") from None


def _fields(data, allowed, required=()):
    if not isinstance(data, dict) or set(data) - set(allowed) or not set(required) <= set(data):
        raise ValueError("Invalid request fields.")
    return data


def _note(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise ValueError("Supply a nonempty note of at most 2000 characters.")
    return value.strip()


class Catalog:
    """Read bounded regular TOML files from a pinned catalog directory."""

    def __init__(self, directory):
        self.directory = Path(directory).absolute()

    @contextmanager
    def _directory(self):
        if any(path.is_symlink() for path in (self.directory, *self.directory.parents)):
            raise ValueError("Recipe catalog paths must not contain symbolic links.")
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            yield fd
        finally:
            os.close(fd)

    def read(self, name):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", name):
            raise ValueError("Select a recipe ID from the catalog.")
        with self._directory() as parent:
            fd = os.open(name + ".toml", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 262144:
                    raise ValueError("Recipes must be regular TOML files at most 256 KiB.")
                raw = stream.read(262145)
                if len(raw) > 262144:
                    raise ValueError("Recipe exceeds the size limit.")
        return tomllib.loads(raw.decode("utf-8"))

    def list(self):
        with self._directory() as parent:
            names = sorted(name[:-5] for name in os.listdir(parent) if name.endswith(".toml"))
        if len(names) > 256:
            raise ValueError("The catalog must contain at most 256 recipes.")
        result = []
        for name in names:
            data = self.read(name)
            # Required parameters need not have defaults. Full validation uses
            # the caller's explicit overrides before any run or acquisition.
            if not isinstance(data.get("name"), str) or not isinstance(data.get("params"), dict):
                raise ValueError(f"Invalid recipe catalog entry: {name}")
            result.append({"id": name, "name": data["name"], "params": data["params"]})
        return result


class ControlLease(Lease):
    def __init__(self, lost, **kwargs):
        super().__init__(on_expire=lambda: lost("Frontend lease expired."), **kwargs)
        self.lost = lost

    def release(self, client):
        # Revoke before availability is published, including same-ID reacquire.
        # control is the public, reentrant short-dispatch lock.
        with self.control(client, require_holder=False):
            if self.snapshot()["holder"] not in (None, client):
                raise ServiceError(409, "Another client holds the control lease.")
            self.lost("Frontend lease released.")
            return super().release(client)


class DemoResources:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.resources = {}
        self.stack = ExitStack()
        self.lock = threading.Lock()

    def clients(self, required, identity):
        from .rehearsal import Rehearsal

        if set(required) - DEMO_SERVICES:
            raise ValueError(
                "Demo supports only robot, stage, thermal, furnace and vision services."
            )
        result = {}
        try:
            with self.lock:
                for name in required:
                    if name not in self.resources:
                        self.resources[name] = self.stack.enter_context(
                            Rehearsal(
                                self.directory / name,
                                (name,),
                                identity="demo-" + uuid.uuid4().hex,
                                **(
                                    {"robot_state_provider": self._robot_state}
                                    if name == "vision"
                                    else {}
                                ),
                            )
                        )
                    result.update(self.resources[name].new_clients(identity))
        except BaseException:
            for client in result.values():
                client.close()
            raise
        return result

    def _robot_state(self):
        resource = self.resources.get("robot")
        return None if resource is None else resource.robot_state

    def calibration_references(self):
        resource = self.resources.get("vision")
        if resource is None or resource.calibration_references_file is None:
            raise ValueError("The owned demo calibration references are unavailable.")
        return resource.calibration_references_file

    def close(self):
        self.stack.close()

    def existing_client(self, name, identity, *, profile="runner"):
        """Read or stop an existing owned demo without starting another service."""
        resource = self.resources.get(name)
        return None if resource is None else resource.new_clients(identity, profile=profile)[name]


class Recovery:
    """A new ownership transaction; its caller retains admission until closed."""

    def __init__(self, run_id, pool, required, resolve):
        self.run_id, self.pool, self.required, self.resolve = run_id, pool, required, resolve
        self.cancelled = threading.Event()
        self.closed = threading.Event()
        self.lock = threading.RLock()
        self.worker = self.cleaner = None
        self.phase, self.error, self.release_errors = "acquiring", None, {}
        self.resolved = False

    def _thread(self, function, name):
        worker = threading.Thread(target=function, name=name, daemon=True)
        worker.start()
        return worker

    def begin(self):
        def acquire():
            try:
                self.pool.acquire(self.required, cancelled=self.cancelled)
                self.pool.stop_all()
                with self.lock:
                    if not self.cancelled.is_set():
                        self.phase = "awaiting-clearance"
            except Exception as error:
                self.error = str(error)
                self.cancel()

        with self.lock:
            try:
                self.worker = self._thread(acquire, "orch-recovery-acquire")
            except Exception as error:
                self.error = str(error)
                self.cancel()
                raise

    def confirm(self, note):
        with self.lock:
            if (
                self.cancelled.is_set()
                or self.phase != "awaiting-clearance"
                or self.worker.is_alive()
            ):
                raise ServiceError(409, "Recovery is not ready for confirmation.")
            self.phase = "confirming"

            def confirm():
                try:
                    self.resolve(self.pool, self.cancelled, note)
                except Exception as error:
                    with self.lock:
                        self.error = str(error)
                        if not self.cancelled.is_set():
                            self.phase = "awaiting-clearance"
                else:
                    with self.lock:
                        self.error = None
                        self.resolved = True
                        self.phase = "resolved"
                    self.cancel()

            try:
                self.worker = self._thread(confirm, "orch-recovery-confirm")
            except Exception as error:
                self.error = str(error)
                self.phase = "awaiting-clearance"
                raise

    def cancel(self):
        with self.pool.lifecycle_lock:
            self.cancelled.set()
        # Never wait for a request/stop while holding the frontend lease lock.
        with self.lock:
            if self.closed.is_set() or self.cleaner is not None:
                return
            self.phase = "closing" if self.phase != "resolved" else "resolved"
            try:
                self.cleaner = self._thread(self._cleanup, "orch-recovery-cleanup")
            except Exception as error:
                # Cancellation is already fenced. Keep admission and let the
                # existing poll/close path retry cleanup, never acquisition.
                self.error = str(error)

    def _cleanup(self):
        initial = self.pool.stop_all()
        while self.worker is not None and self.worker.is_alive():
            self.pool.stop_all(initial)
            time.sleep(0.02)
        while not self.pool.round_done(initial):
            self.pool.stop_all(initial)
            time.sleep(0.02)
        final = self.pool.stop_all()
        while not self.pool.round_done(final):
            self.pool.stop_all(final)
            time.sleep(0.02)
        while (result := self.pool.release()) is False:
            time.sleep(0.02)
        with self.lock:
            self.release_errors = result
            self.phase = "closed"
            self.closed.set()

    def snapshot(self):
        with self.lock:
            result = {
                "run_id": self.run_id,
                "phase": self.phase,
                "error": self.error,
                "cancelled": self.cancelled.is_set(),
                "closed": self.closed.is_set(),
                "resolved": self.resolved,
                "release_errors": dict(self.release_errors),
            }
        result["stops"] = self.pool.stop_snapshot()
        return result


class OrchestratorService:
    def __init__(
        self,
        *,
        settings=None,
        recipes_dir=None,
        runs_dir=None,
        demo=False,
        registry=None,
        client_factory=None,
        runner_factory=Runner,
        clock=time.monotonic,
    ):
        self.settings = dict(os.environ if settings is None else settings)
        self.catalog = Catalog(recipes_dir or CATALOG_DEFAULT)
        self.runs_dir = Path(runs_dir or DEFAULT_RUNS)
        self.demo = bool(demo)
        self.registry = registry or default_registry(
            calibration_reference_provider=self._demo_calibration_references if self.demo else None
        )
        self.client_factory, self.runner_factory = client_factory, runner_factory
        self.events = EventLog()
        self.lease = ControlLease(self._lost, clock=clock)
        self.lock = threading.RLock()
        self.store = self.resources = self.temporary = None
        self.current = self.recovery = self.job = None
        self.runners, self.plans = {}, {}
        self.owner = None
        self.resume_cancel = None
        self.closing = self.started = False
        self.last_command = self.fault = None
        self.report = None
        self.poll_stop = threading.Event()
        self.poller = None
        from .web.backend import Dashboard
        from .web.console import Console

        self.dashboard = Dashboard(self, clock=clock)
        self.console = Console(self, clock=clock)

    def _demo_calibration_references(self):
        if not self.demo or self.resources is None:
            raise ValueError("The owned demo calibration references are unavailable.")
        return self.resources.calibration_references()

    def start(self, *, background=True):
        with self.lock:
            if self.started or self.closing:
                raise RuntimeError("The service can start only once.")
            if self.demo:
                if self.client_factory is None and not demo_isolated(os.environ):
                    raise ValueError("Start real demos through the isolated serve --demo process.")
                self.temporary = tempfile.TemporaryDirectory(prefix="mg400-orch-demo-")
                root = Path(self.temporary.name)
                zones = root / "zones"
                zones.mkdir(mode=0o750)
                zones.chmod(0o750)
                self.settings[STATE_SETTING] = str(zones / "occupancy.json")
                if not self.settings.get(CONFIG_SETTING):
                    config = zones / "config.toml"
                    config.write_text("[zones]\n")
                    config.chmod(0o600)
                    self.settings[CONFIG_SETTING] = str(config)
                self.runs_dir = root / "runs"
                if self.client_factory is None:
                    self.resources = DemoResources(root / "demos")
            self.store = RunStore(self.runs_dir)
            self.started = True
            if background:
                self.poller = threading.Thread(
                    target=self._poll, name="orch-service-poll", daemon=True
                )
                try:
                    self.poller.start()
                except BaseException:
                    self.poller = None
                    raise
        return self

    def _poll(self):
        while not self.poll_stop.wait(0.05):
            try:
                self.poll_once()
            except Exception as error:
                self.fault = str(error)
                self._lost("Service polling failed.")

    def poll_once(self):
        self.lease.poll()
        self.dashboard.poll()
        with self.lock:
            recovery = self.recovery
        if (
            recovery is not None
            and not recovery.closed.is_set()
            and (recovery.pool.error or recovery.cancelled.is_set())
        ):
            recovery.cancel()
        self._refresh()

    def _refresh(self):
        # Snapshot reads occupancy files. Only the poll/job/stop paths call this,
        # never contract snapshot() under the frontend short-dispatch lease lock.
        with self.lock:
            runner = self.current
        report = runner.snapshot() if runner is not None else None
        with self.lock:
            if runner is self.current:
                self.report = report

    def describe(self):
        return describe(
            service="orchestrator",
            version="1",
            demo=self.demo,
            zones=[],
            lease_seconds=self.lease.seconds,
            on_lease_expiry="Hold the active run and stop motion; cancel terminal recovery.",
            commands={"hold": command(), "abort": command()},
        )

    def _occupied(self):
        return (
            self.dashboard.busy()
            or self.job is not None
            and not self.job["done"].is_set()
            or self.current is not None
            and not self.current.wait_closed(0)
            or self.recovery is not None
            and not self.recovery.closed.is_set()
        )

    def snapshot(self):
        with self.lock:
            recovery = self.recovery
            busy, closing, started = bool(self._occupied()), self.closing, self.started
            last, fault = copy.deepcopy(self.last_command), self.fault
            active = copy.deepcopy(self.report)
            pending = (
                None
                if self.job is None
                else {
                    "id": self.job["id"],
                    "action": self.job["action"],
                    "done": self.job["done"].is_set(),
                    "error": self.job.get("error"),
                }
            )
        retained = recovery.snapshot() if recovery is not None else None
        held = bool(active and active["status"] == "held" and not active["closed"])
        return {
            "service": "orchestrator",
            "seq": self.events.since()["next"],
            "ts_unix": time.time(),
            "demo": self.demo,
            "connected": started and not closing,
            "busy": busy,
            "locked": bool(fault or held or closing),
            "fault": {"code": "service-error", "message": fault} if fault else None,
            "lease": self.lease.snapshot(),
            "last_command": last,
            "active_run": active,
            "operation": pending,
            "recovery": retained,
            "closing": closing,
        }

    def _ready(self):
        if not self.started or self.closing:
            raise ServiceError(409, "The service is not accepting work.")
        if self.fault:
            raise ServiceError(409, "Resolve the service publication fault before new control.")

    def _complete(self, job, error=None):
        record = {
            "id": job["id"],
            "action": job["action"],
            "client": job["client"],
            "result": "failed" if error is not None else "ok",
            "message": str(error) if error is not None else "Completed.",
            "finished_at": time.time(),
        }
        try:
            self.events.add(
                "error" if error is not None else "info", record["message"], command_id=job["id"]
            )
        except Exception as publication_error:
            error = publication_error
            record.update(result="failed", message=str(error))
            with self.lock:
                self.fault = f"Command publication failed: {error}"
                self._lost(self.fault)
        finally:
            with self.lock:
                job["error"] = str(error) if error is not None else None
                self.last_command = record
                job["done"].set()

    def _schedule(self, action, client, function, *, exclusive=False):
        # Caller holds lease->app locks only for admission and thread dispatch.
        self._ready()
        if self.dashboard.busy():
            raise ServiceError(409, "Dashboard safety requests are still draining.")
        if self.job is not None and not self.job["done"].is_set():
            raise ServiceError(409, "Another control operation is still draining.")
        if exclusive and self._occupied():
            raise ServiceError(409, "The current run or recovery still owns admission.")
        job = {
            "id": uuid.uuid4().hex,
            "action": action,
            "client": client,
            "done": threading.Event(),
            "cancelled": threading.Event(),
        }
        self.job, self.owner = job, client

        def work():
            try:
                if job["cancelled"].is_set():
                    raise RuntimeError("The operation was cancelled before dispatch.")
                function(job)
                self._refresh()
            except Exception as error:
                self._complete(job, error)
            else:
                self._complete(job)

        worker = threading.Thread(target=work, name="orch-" + action, daemon=True)
        job["worker"] = worker
        try:
            worker.start()
        except Exception as error:
            # No accepted worker exists. Do not log or wait while dispatch owns
            # the frontend lease; retain an explicit failed result locally.
            self.last_command = {
                "id": job["id"],
                "action": action,
                "client": client,
                "result": "failed",
                "message": str(error),
                "finished_at": time.time(),
            }
            job["error"] = str(error)
            job["done"].set()
            raise
        return job["id"]

    def _clients(self, required, identity):
        if self.client_factory is not None:
            services = self.client_factory(required, identity)
        elif self.resources is not None:
            services = self.resources.clients(required, identity)
        else:
            services = from_settings(self.settings, identity=identity)
        if set(required) - set(services):
            raise ValueError("A required service is not configured.")
        return {name: services[name] for name in required}

    def dashboard_configured(self, name):
        """Report configured endpoints without starting a simulator or acquiring control."""
        if self.client_factory is not None:
            return True
        if self.demo:
            return self.resources is not None and name in self.resources.resources
        return bool(self.settings.get(f"MG400_ORCH_{name.upper()}_URL"))

    def dashboard_client(self, name, identity, *, profile="runner"):
        """Return a fresh read/stop transport; never acquire or renew a lease."""
        from .clients import SERVICES

        if name not in SERVICES:
            raise ValueError("Unknown dashboard service.")
        if self.client_factory is not None:
            return self.client_factory((name,), identity).get(name)
        if self.demo:
            return (
                None
                if self.resources is None
                else self.resources.existing_client(name, identity, profile=profile)
            )
        key = f"MG400_ORCH_{name.upper()}_URL"
        settings = {key: self.settings[key]} if self.settings.get(key) else {}
        return from_settings(settings, identity=identity).get(name)

    def console_client(self, name, identity):
        """Return a fresh console transport. Only a console Take control acquires with it."""
        return self.dashboard_client(name, identity, profile="console")

    def console_start_demo(self, name):
        """Start one owned demo service for an explicit console Take control."""
        if not self.demo or self.resources is None or name not in DEMO_SERVICES:
            return
        with self.lock:
            if self.closing:
                raise ServiceError(409, "The service is shutting down.")
        for client in self.resources.clients((name,), "console-demo-" + uuid.uuid4().hex).values():
            client.close()

    def dashboard_summary(self):
        """Read run/storage state only from a dashboard background worker."""
        state = self.snapshot()
        with self.lock:
            runner = self.current
        return state, self.get_run(runner.run_id) if runner is not None else None

    def dashboard_drain(self, context):
        """Request hold outside app locks and inspect every accepted old operation.

        The dashboard gate is already published. Initial instrument stops run
        independently even if a preexisting release holds this app lock.
        """
        with self.lock:
            job, runner, recovery = self.job, self.current, self.recovery
            cancelled = self.resume_cancel
            if job is not None:
                job["cancelled"].set()
            self.owner = None
        if runner is not None and (
            context.get("runner") is not runner or context.get("resume") is not cancelled
        ):
            if cancelled is not None:
                runner.cancel_resume(cancelled, "Dashboard safety hold requested.")
            else:
                runner.hold("Dashboard safety hold requested.")
            context.update(runner=runner, resume=cancelled)
        if recovery is not None and not recovery.closed.is_set():
            recovery.cancel()
        if job is not None and not job["done"].is_set():
            return {"settled": False, "confirmed": False}
        if recovery is not None and not recovery.closed.is_set():
            return {"settled": False, "confirmed": False}
        report = runner.snapshot() if runner is not None else None
        with self.lock:
            if runner is not self.current or job is not self.job or recovery is not self.recovery:
                return {"settled": False, "confirmed": False}
        settled = (
            report is None
            or report.get("closed") is True
            or (
                report.get("status") == "held"
                and report.get("settled") is True
                and report.get("draining") is False
            )
        )
        confirmed = not report or not (
            report.get("unconfirmed_services")
            or report.get("release_errors")
            or report.get("persistence_error")
        )
        if recovery is not None:
            confirmed = confirmed and all(
                result.get("stop") == "confirmed"
                for result in recovery.snapshot()["stops"].values()
            )
        return {"settled": settled, "confirmed": bool(confirmed)}

    def prepare_run(self, data):
        _fields(data, {"recipe", "params"}, {"recipe"})
        if not isinstance(data.get("params", {}), dict):
            raise ValueError("Recipe params must be an object.")
        recipe = validate(self.catalog.read(data["recipe"]), self.registry, data.get("params"))
        plan = prepare_zones(recipe, self.registry, self.settings)
        if self.demo and set(plan.required_services) - DEMO_SERVICES:
            raise ValueError(
                "Demo supports only robot, stage, thermal, furnace and vision services."
            )
        return recipe, plan

    def run(self, prepared, client, *, borrow=False):
        """Start a run. With borrow, the run uses the devices that this client's
        console page holds, and the console keeps their leases."""
        recipe, plan = prepared
        lent = self.console.lend(client, plan.required_services) if borrow else None

        def start(job):
            if lent is not None:
                services = lent
            else:
                services = self._clients(plan.required_services, "run-" + uuid.uuid4().hex)
            if job["cancelled"].is_set():
                for service in services.values():
                    service.close()
                raise RuntimeError("Run creation was cancelled.")
            try:
                runner = self.runner_factory(
                    recipe, self.registry, services, self.store, zone_plan=plan
                )
            except BaseException:
                for service in services.values():
                    service.close()
                raise
            with self.lock:
                self.current = runner
                self.runners[runner.run_id] = runner
                self.plans[runner.run_id] = plan
                cancelled = self.closing or job["cancelled"].is_set()
            if cancelled:
                runner.shutdown("Run stopped during startup.")
            else:
                runner.start()

        try:
            with self.lease.control(client), self.lock:
                return self._schedule("run", client, start, exclusive=True)
        except BaseException:
            for service in (lent or {}).values():
                service.close()
            raise

    def get_run(self, run_id):
        result = self.store.get(run_id)
        with self.lock:
            runner = self.runners.get(run_id)
            recovery = self.recovery if self.recovery and self.recovery.run_id == run_id else None
        return {
            **result,
            "historical": runner is None,
            "live": runner.snapshot() if runner else None,
            "recovery": recovery.snapshot() if recovery else None,
        }

    def _runner(self, run_id):
        runner = self.runners.get(run_id)
        if runner is None or runner is not self.current or runner.wait_closed(0):
            raise ServiceError(409, "This run is historical or has completed cleanup.")
        return runner

    def resume(self, run_id, data, client):
        _fields(
            data, {"manually_completed", "note", "physically_clear"}, {"manually_completed", "note"}
        )
        if (
            data["manually_completed"] is not True
            or type(data.get("physically_clear", False)) is not bool
        ):
            raise ValueError("Confirm manual completion and use a boolean physical declaration.")
        note = _note(data["note"])
        with self.lease.control(client), self.lock:
            runner = self._runner(run_id)
            cancelled = threading.Event()

            def resume(job):
                runner.resume(
                    manually_completed=True,
                    note=note,
                    physically_clear=data.get("physically_clear", False),
                    cancelled=cancelled,
                )

            command_id = self._schedule("resume", client, resume)
            self.resume_cancel = cancelled
            return command_id

    def abort(self, run_id, client):
        with self.lease.control(client), self.lock:
            runner = self._runner(run_id)
            if self.resume_cancel is not None:
                runner.cancel_resume(self.resume_cancel, "Run aborted.")
            runner.abort()
            return self._record("abort", client)

    def _record(self, action, client):
        command_id = uuid.uuid4().hex
        self.last_command = {
            "id": command_id,
            "action": action,
            "client": client,
            "result": "ok",
            "message": "Requested.",
            "finished_at": time.time(),
        }
        return command_id

    def _lost(self, reason):
        with self.lock:
            if self.job is not None:
                self.job["cancelled"].set()
            if self.current is not None and not self.current.wait_closed(0):
                if self.resume_cancel is not None:
                    self.current.cancel_resume(self.resume_cancel, reason)
                else:
                    self.current.hold(reason)
            if self.recovery is not None and not self.recovery.closed.is_set():
                self.recovery.cancel()
            self.owner = None

    def on_lease_expired(self):
        self._lost("Frontend lease expired.")

    def command(self, action, args, client):
        _fields(args, ())
        with self.lock:
            self._ready()
            if action == "hold":
                self._lost("Explicit hold requested.")
            elif action == "abort" and self.current is not None:
                self.current.abort()
            else:
                raise ValueError("Unknown command or no active run.")
            return self._record(action, client)

    def stop(self, client, wait_s=0):
        validate_client(client)
        if type(wait_s) not in {int, float} or not 0 <= wait_s <= 15 or not math.isfinite(wait_s):
            raise ValueError("wait_s must be between 0 and 15.")
        self._lost("Stop requested.")
        deadline = time.monotonic() + wait_s
        while True:
            self._refresh()
            report = self.snapshot()
            active, recovery = report["active_run"], report["recovery"]
            pending = bool(report["operation"] and not report["operation"]["done"])
            pending |= self.dashboard.busy()
            pending |= bool(recovery and not recovery["closed"])
            pending |= bool(active and not active["closed"] and not active["settled"])
            if not pending:
                unconfirmed = bool(active and active["unconfirmed_services"])
                unconfirmed |= bool(
                    recovery
                    and any(item.get("stop") != "confirmed" for item in recovery["stops"].values())
                )
                return "unconfirmed" if unconfirmed else "confirmed"
            if time.monotonic() >= deadline:
                return "pending"
            time.sleep(0.02)

    def begin_recovery(self, run_id, client):
        self.store.get(run_id)  # Require an existing history record, never replay it.
        with self.lock:
            runner = self.runners.get(run_id)
            saved_plan = self.plans.get(run_id)
        if runner is not None:
            if not runner.wait_closed(0):
                raise ServiceError(409, "Use live resume before this run finishes cleanup.")
            plan = saved_plan
            zones = None
        else:
            plan = prepare_zones(Recipe("Recovery", 1, {}, (), ()), self.registry, self.settings)
            zones = Zones(
                plan,
                {},
                holder=run_id,
                audit=lambda kind, data: self.store.event(run_id, kind, data),
            )
            claims = zones.owned()
            if not claims:
                raise ServiceError(409, "This historical run has no retained zones.")
            required = tuple(
                sorted({name for claim in claims.values() for name in claim["services"]})
            )
            plan = replace(plan, required_services=required)
        if self.demo and set(plan.required_services) - DEMO_SERVICES:
            raise ValueError("No owned demo exists for a required recovery service.")

        def begin(job):
            services = self._clients(plan.required_services, "recovery-" + uuid.uuid4().hex)
            pool = Clients(services)

            def resolve(pool, cancelled, note):
                if runner is not None:
                    report = runner.recover_terminal(
                        pool, physically_clear=True, note=note, cancelled=cancelled
                    )
                    if report["zone_error"] or any(
                        claim["holder"] == run_id for claim in report["zones"].values()
                    ):
                        raise RuntimeError("Zone recovery is incomplete.")
                else:

                    @contextmanager
                    def guard():
                        with pool.lifecycle_lock:
                            if cancelled.is_set() or pool.error:
                                raise RuntimeError("Recovery lost permission to publish.")
                            yield

                    zones.recover(
                        run_id,
                        pool.services,
                        physically_clear=True,
                        note=note,
                        live=False,
                        commit_guard=guard,
                    )
                    if zones.error or zones.owned():
                        raise RuntimeError("Zone recovery is incomplete.")

            recovery = Recovery(run_id, pool, plan.required_services, resolve)
            with self.lock:
                self.recovery = recovery
                if job["cancelled"].is_set() or self.closing:
                    recovery.cancel()
                else:
                    recovery.begin()
            if recovery.worker is not None:
                recovery.worker.join()
            if recovery.error or recovery.cancelled.is_set():
                raise RuntimeError(recovery.error or "Recovery acquisition was cancelled.")

        with self.lease.control(client), self.lock:
            return self._schedule("recovery", client, begin, exclusive=True)

    def confirm_recovery(self, run_id, data, client):
        _fields(data, {"physically_clear", "note"}, {"physically_clear", "note"})
        if data["physically_clear"] is not True:
            raise ValueError("Declare physical clearance of all retained zones.")
        note = _note(data["note"])
        with self.lease.control(client), self.lock:
            self._ready()
            recovery = self.recovery
            if recovery is None or recovery.run_id != run_id:
                raise ServiceError(409, "Begin a new recovery transaction first.")

            def confirm(job):
                recovery.confirm(note)
                recovery.worker.join()
                if not recovery.resolved:
                    raise RuntimeError(recovery.error or "Recovery confirmation was cancelled.")

            return self._schedule("recovery-confirm", client, confirm)

    def abort_recovery(self, run_id, client):
        with self.lease.control(client), self.lock:
            recovery = self.recovery
            if recovery is None or recovery.run_id != run_id:
                raise ServiceError(409, "No matching recovery transaction.")
            recovery.cancel()
            return self._record("recovery-abort", client)

    def close(self):
        self.dashboard.begin_close()
        with self.lock:
            self.closing = True
            self._lost("Orchestrator service is shutting down.")
            runner = self.current
            if runner is not None:
                runner.shutdown("Orchestrator service is shutting down.")
        while True:
            with self.lock:
                runner, recovery, job = self.current, self.recovery, self.job
            if runner is not None and not runner.wait_closed(0):
                runner.shutdown("Orchestrator service is shutting down.")
            if recovery is not None and not recovery.closed.is_set():
                recovery.cancel()
            if (
                (job is None or job["done"].is_set())
                and (runner is None or runner.wait_closed(0))
                and (recovery is None or recovery.closed.is_set())
            ):
                break
            time.sleep(0.02)
        # After the drain: the run has returned its lent leases, so the console releases them.
        self.console.close()
        self.poll_stop.set()
        if self.poller is not None:
            self.poller.join()
        self.dashboard.close()
        if self.resources is not None:
            self.resources.close()
            self.resources = None
        if self.store is not None:
            self.store.close()
            self.store = None
        if self.temporary is not None:
            self.temporary.cleanup()
            self.temporary = None


def create_app(service, *, base_path="/"):
    app = Flask(__name__)
    app.json = StrictJSON(app)
    app.config["MAX_CONTENT_LENGTH"] = 65536
    app.register_blueprint(contract_blueprint(service, base_path=base_path, lease=service.lease))
    prefix = base_path.rstrip("/")
    from .web import console_blueprints, dashboard_blueprint

    app.register_blueprint(dashboard_blueprint(service, prefix))
    for blueprint in console_blueprints(service, prefix):
        app.register_blueprint(blueprint)

    @app.before_request
    def access():
        if request.method == "POST":
            if request.mimetype != "application/json":
                raise ServiceError(415, "Expected Content-Type: application/json.")
            validate_client(request.headers.get("X-MG400-Client"))
            if not isinstance(request.get_json(), dict):
                raise ValueError("Expected a JSON object.")

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.errorhandler(ServiceError)
    def service_error(error):
        return jsonify(error=error.message), error.status

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(RuntimeError)
    def conflict(error):
        return jsonify(error=str(error)), 409

    @app.errorhandler(OSError)
    def storage(error):
        return jsonify(error="Configured catalog or run storage is unavailable."), 503

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @app.get(prefix + "/api/recipes")
    def recipes():
        return jsonify(recipes=service.catalog.list())

    @app.get(prefix + "/api/runs")
    def runs():
        return jsonify(runs=service.store.runs())

    @app.post(prefix + "/api/runs")
    def run():
        data = request.get_json()
        borrow = data.pop("borrow", False) if isinstance(data, dict) else False
        if type(borrow) is not bool:
            raise ValueError("borrow must be true or false.")
        prepared = service.prepare_run(data)
        command_id = service.run(prepared, request.headers["X-MG400-Client"], borrow=borrow)
        return jsonify(command_id=command_id), 202

    @app.get(prefix + "/api/runs/<run_id>")
    def get_run(run_id):
        return jsonify(service.get_run(run_id))

    @app.post(prefix + "/api/runs/<run_id>/resume")
    def resume(run_id):
        return jsonify(
            command_id=service.resume(run_id, request.get_json(), request.headers["X-MG400-Client"])
        ), 202

    @app.post(prefix + "/api/runs/<run_id>/abort")
    def abort(run_id):
        _fields(request.get_json(), ())
        return jsonify(command_id=service.abort(run_id, request.headers["X-MG400-Client"])), 202

    @app.post(prefix + "/api/runs/<run_id>/recovery")
    def recovery(run_id):
        _fields(request.get_json(), ())
        return jsonify(
            command_id=service.begin_recovery(run_id, request.headers["X-MG400-Client"])
        ), 202

    @app.post(prefix + "/api/runs/<run_id>/recovery/confirm")
    def confirm(run_id):
        return jsonify(
            command_id=service.confirm_recovery(
                run_id, request.get_json(), request.headers["X-MG400-Client"]
            )
        ), 202

    @app.post(prefix + "/api/runs/<run_id>/recovery/abort")
    def cancel(run_id):
        _fields(request.get_json(), ())
        return jsonify(
            command_id=service.abort_recovery(run_id, request.headers["X-MG400-Client"])
        ), 202

    return app
