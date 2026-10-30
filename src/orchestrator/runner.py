"""Sequential recipes with permanently revoked failed-step contexts."""

import copy
import logging
import threading
import time
from contextlib import nullcontext
from types import MappingProxyType

from .clients import Clients
from .recipe import positive


class Revoked(RuntimeError):
    pass


class _Authority:
    def __init__(self, deadline, *, failure=lambda: None, lifecycle_lock=None):
        self.deadline = deadline
        self.cancelled = threading.Event()
        self._lock = lifecycle_lock or threading.RLock()
        self._failure = failure
        self.active = 0

    def revoke(self):
        with self._lock:
            self.cancelled.set()

    def check(self):
        with self._lock:
            if self.cancelled.is_set() or time.monotonic() >= self.deadline or self._failure():
                raise Revoked("This step no longer has permission to act.")

    @property
    def drained(self):
        with self._lock:
            return self.active == 0

    def call(self, function, *args, **kwargs):
        # Registration is atomic with revocation. A call already in flight is
        # reconciled after it drains; no later call can use this authority.
        with self._lock:
            self.check()
            self.active += 1
        try:
            result = function(*args, **kwargs)
            self.check()
            return result
        finally:
            with self._lock:
                self.active -= 1


class StepClient:
    """Expose guarded operations only, never the underlying service client."""

    __slots__ = ("__call", "__remaining")

    def __init__(self, authority, service):
        def invoke(method, *args, **kwargs):
            return authority.call(getattr(service, method), *args, **kwargs)

        self.__call = invoke
        self.__remaining = lambda: max(0.000001, authority.deadline - time.monotonic())

    def state(self):
        return copy.deepcopy(self.__call("state"))

    def owned(self, state):
        """Check this run's ownership through the same revocable authority."""
        return self.__call("owned", state)

    def run(self, action, **params):
        self.__ordinary(action)
        return self.__call("run", action, timeout=self.__remaining(), **params)

    def command(self, action, **params):
        self.__ordinary(action)
        return self.__call("command", action, **params)

    @staticmethod
    def __ordinary(action):
        if action in {"start_sync", "stop_sync", "clear_alarm", "disable"}:
            raise ValueError("Skills cannot change ownership or perform manual lock recovery.")

    def stop(self):
        return self.__call("stop")


class StepStore:
    __slots__ = ("__sample", "__artifact", "__event")

    def __init__(self, authority, store, run_id, position, chip_id):
        self.__sample = lambda identity, data: authority.call(
            store.sample, run_id, position, identity, data
        )
        self.__artifact = lambda name, data, identity: authority.call(
            store.artifact,
            run_id,
            position,
            name,
            data,
            chip_id=identity if identity is not None else chip_id,
        )
        self.__event = lambda kind, data: authority.call(
            store.event, run_id, kind, data, chip_id=chip_id, step=position
        )

    def sample(self, chip_id, data):
        return self.__sample(chip_id, data)

    def artifact(self, name, data, *, chip_id=None):
        return self.__artifact(name, data, chip_id)

    def event(self, kind, data):
        return self.__event(kind, data)


class NoZones:
    """Placeholder until ORC-04. This object provides no cross-device interlock."""

    def take(self, name):
        return nullcontext()


class Context:
    def __init__(self, authority, services, store, run_id, position, chip_id, time_scale):
        self.clients = MappingProxyType(
            {name: StepClient(authority, service) for name, service in services.items()}
        )
        self.store = StepStore(authority, store, run_id, position, chip_id)
        self.zones = NoZones()
        self.__authority = authority
        self.__time_scale = time_scale

    def check(self):
        self.__authority.check()

    def sleep(self, seconds):
        if seconds < 0:
            raise ValueError("Wait duration must not be negative.")
        self.check()
        remaining = min(
            seconds * self.__time_scale, max(0, self.__authority.deadline - time.monotonic())
        )
        self.__authority.cancelled.wait(remaining)
        self.check()


class Runner:
    """A run owns one coordinator and at most one skill worker at a time.

    Held work retains leases. A human can resume only after manual completion,
    worker drain, a final stop, and service reconciliation. Abort remains pending
    until old work drains; it never releases ownership under a live old worker.
    """

    def __init__(self, recipe, registry, services, store, *, time_scale=1):
        recipe = copy.deepcopy(recipe)
        self.recipe, self.registry, self.store = recipe, registry, store
        self.time_scale = positive(time_scale, "time scale")
        # Recheck the complete prepared recipe before starting a client or worker.
        for step in recipe.steps:
            registry[step.skill].validate(step.params)
            positive(step.timeout_s, "timeout_s")
            if step.on_fail not in {"hold", "abort"}:
                raise ValueError("on_fail must be hold or abort.")
        required = {name for step in recipe.steps for name in registry[step.skill].services}
        if set(recipe.services) != required:
            raise ValueError("Recipe services do not match the registered skills.")
        if set(recipe.services) - set(services):
            raise ValueError("A required service is not configured.")
        self.clients = Clients(services)
        self.run_id = store.create(recipe)
        self._condition = threading.Condition(threading.RLock())
        self._status = "created"
        self._abort = False
        self._cancel_run = threading.Event()
        self._resume = None
        self._worker = self._thread = self._authority = None
        self._position = 0
        self._settled = False
        self._resolving = False
        self._final_round = None
        self._failure = ""
        self.persistence_error = None
        self.release_errors = {}

    @property
    def status(self):
        with self._condition:
            return self._status

    def snapshot(self):
        """Report safety outcomes even when the persistent audit store failed."""
        with self._condition:
            stops = self.clients.stop_snapshot()
            return {
                "run_id": self.run_id,
                "status": self._status,
                "step": self._position,
                "detail": self._failure,
                "persistence_error": self.persistence_error,
                "stops": stops,
                "unconfirmed_services": sorted(
                    name
                    for name in self.clients.acquired
                    if stops.get(name, {}).get("stop") != "confirmed"
                ),
                "release_errors": dict(self.release_errors),
            }

    def _set(self, status, detail=""):
        with self._condition:
            self._persist("status", self.run_id, status, detail)
            self._status = "aborted" if status == "completed" and self.persistence_error else status
            self._condition.notify_all()

    def _persist(self, method, *args, **kwargs):
        """Audit failure must revoke work, never bypass drain and stop cleanup."""
        try:
            getattr(self.store, method)(*args, **kwargs)
            return True
        except Exception as exc:
            with self._condition:
                self.persistence_error = self.persistence_error or str(exc)
                self._failure = f"Run persistence failed: {self.persistence_error}"
                self._abort = True
                self._cancel_run.set()
                if self._authority is not None:
                    self._authority.revoke()
                self._condition.notify_all()
            try:
                logging.getLogger(__name__).error(self._failure)
            except Exception:
                pass
            return False

    def start(self):
        with self._condition:
            if self._thread is not None or self._status != "created":
                raise RuntimeError("A run can start only once.")
            try:
                self._thread = threading.Thread(target=self._run, name="orch-run", daemon=True)
                self._thread.start()
            except Exception as exc:
                self._abort = True
                self._failure = str(exc)
                self._set("aborted", self._failure)
                raise
        return self

    def wait(self, timeout=30):
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._status not in {"held", "completed", "aborted"}:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("The run is still active.")
                self._condition.wait(remaining)
            return self._status

    def abort(self):
        with self._condition:
            if self._status in {"completed", "aborted"}:
                return False
            self._abort = True
            self._cancel_run.set()
            if self._authority is not None:
                self._authority.revoke()
            self._condition.notify_all()
            return True

    def resume(self, *, manually_completed=False, note=""):
        with self._condition:
            if self._status != "held" or self._abort:
                raise RuntimeError("Only a held run can resume.")
            if manually_completed is not True or not isinstance(note, str) or not note.strip():
                raise ValueError("Confirm manual completion and supply a resolution note.")
            if not self._settled or not self._drained() or self._resolving:
                raise RuntimeError("The failed worker and final stop must finish before recovery.")
            self._resolving = True
            self._settled = False
        # Old work has drained. Require a fresh stop or verified explicit manual
        # disable/rest; cached confirmation alone cannot authorize recovery.
        round_id = None
        try:
            round_id = self._stop(recovery=True)
            states = self.clients.reconcile()
            with self._condition:
                if self._abort:
                    raise RuntimeError("The run was aborted during reconciliation.")
                if not self._persist(
                    "event",
                    self.run_id,
                    "manual-resolution",
                    {"note": note, "states": states},
                    chip_id=self._chip_id(),
                    step=self._position,
                ):
                    raise RuntimeError("Cannot record the manual resolution.")
                self._resume = note
        finally:
            with self._condition:
                if round_id is not None:
                    self._final_round = round_id
                    self._settled = self.clients.round_done(round_id)
                self._resolving = False
                self._condition.notify_all()

    def _chip_id(self):
        if self._position < len(self.recipe.steps):
            return self.recipe.steps[self._position].params.get(
                "chip_id", self.recipe.params.get("chip_id")
            )
        return self.recipe.params.get("chip_id")

    def _drained(self):
        return (self._worker is None or not self._worker.is_alive()) and (
            self._authority is None or self._authority.drained
        )

    def _stop(self, round_id=None, *, recovery=False):
        result = self.clients.recovery_stop_all() if recovery else self.clients.stop_all(round_id)
        self._persist(
            "event",
            self.run_id,
            "stop",
            self.clients.stop_snapshot(),
            chip_id=self._chip_id(),
            step=self._position,
        )
        return result

    def _hold(self, error, *, abort=False):
        with self._condition:
            if self._authority is not None:
                self._authority.revoke()
            self._failure = str(error)
            if abort:
                self._abort = True
                self._cancel_run.set()
            self._settled = False
        was_drained = self._drained()
        initial_round = self._stop()
        with self._condition:
            self._final_round = initial_round if was_drained else None
            self._set("aborting" if self._abort else "held", self._failure)
        while True:
            with self._condition:
                settling = self._drained() and not self._settled and not self._resolving
                final_round = self._final_round
            if settling:
                if final_round is None:
                    final_round = self._stop()
                elif not self.clients.round_done(final_round):
                    # Each service advances independently. One blocked stop must
                    # not delay a final stop on another service after a late write.
                    self._stop(final_round)
                with self._condition:
                    self._final_round = final_round
                    self._settled = self.clients.round_done(final_round)
            with self._condition:
                if self._abort:
                    if self._status != "aborting":
                        self._set("aborting", self._failure)
                    if self._settled and not self._resolving:
                        return False
                elif self._resume is not None and not self._resolving:
                    note, self._resume = self._resume, None
                    # Recheck immediately before the next step. A lease/fault may
                    # change after the human submitted a resolution.
                    try:
                        self.clients.reconcile()
                    except Exception as exc:
                        self._failure = str(exc)
                        self._set("held", self._failure)
                    else:
                        if self._position < len(self.recipe.steps):
                            if not self._persist(
                                "step",
                                self.run_id,
                                self._position,
                                "manually_completed",
                                error=str(error),
                                resolution=note,
                            ):
                                return False
                        return True
                self._condition.wait(0.05)

    def _step(self, step):
        with self._condition, self.clients.lifecycle_lock:
            if self._abort or self.clients.error:
                return RuntimeError(self.clients.error or "A person aborted the run.")
            self._authority = _Authority(
                time.monotonic() + step.timeout_s,
                failure=lambda: self.clients.error,
                lifecycle_lock=self.clients.lifecycle_lock,
            )
            services = {
                name: self.clients.services[name] for name in self.registry[step.skill].services
            }
            ctx = Context(
                self._authority,
                services,
                self.store,
                self.run_id,
                self._position,
                self._chip_id(),
                self.time_scale,
            )
        outcome = {}

        def invoke():
            try:
                outcome["result"] = self.registry[step.skill].function(
                    ctx, **copy.deepcopy(step.params)
                )
                ctx.check()
            except BaseException as exc:
                outcome["error"] = exc

        with self._condition, self.clients.lifecycle_lock:
            if self._abort or self.clients.error:
                self._authority.revoke()
                return RuntimeError(self.clients.error or "A person aborted the run.")
            if not self._persist("step", self.run_id, self._position, "running"):
                return RuntimeError("Cannot record step startup.")
            try:
                self._worker = threading.Thread(target=invoke, name="orch-skill", daemon=True)
                self._worker.start()
            except Exception as exc:
                self._authority.revoke()
                return exc
        while self._worker.is_alive():
            if self._abort:
                return RuntimeError("A person aborted the run.")
            if self.clients.error:
                return RuntimeError(self.clients.error)
            if time.monotonic() >= self._authority.deadline:
                return TimeoutError("The step timed out. It will not be retried.")
            self._worker.join(0.02)
        self._authority.revoke()
        if not self._authority.drained:
            return RuntimeError("The skill returned with service operations still in flight.")
        if self._abort or self.clients.error:
            return RuntimeError(self.clients.error or "A person aborted the run.")
        if "error" in outcome:
            return outcome["error"]
        if not self._persist(
            "step", self.run_id, self._position, "completed", result=outcome.get("result")
        ):
            return RuntimeError("Cannot record step completion.")
        return None

    def _final_cleanup(self):
        # This guard also runs after an unexpected coordinator error. Audit
        # failures cannot jump directly to lease release with old writes alive.
        with self._condition:
            if self._authority is not None:
                self._authority.revoke()
        drained = self._drained()
        round_id = self._stop()
        while not self._drained():
            time.sleep(0.02)
        if not drained:
            round_id = self._stop()
        while not self.clients.round_done(round_id):
            self._stop(round_id)
            time.sleep(0.02)
        if not self.clients.confirmed:
            self._failure = (
                self._failure or "A service stop remains unconfirmed. Manual recovery is required."
            )
        return self.clients.confirmed

    def _run(self):
        completed = False
        try:
            self._set("acquiring")
            try:
                self.clients.acquire(self.recipe.services, cancelled=self._cancel_run)
            except Exception as exc:
                self._hold(exc, abort=True)
                return
            while self._position < len(self.recipe.steps):
                self._set("running")
                step = self.recipe.steps[self._position]
                error = self._step(step)
                if error is not None:
                    self._persist("step", self.run_id, self._position, "failed", error=str(error))
                    if not self._hold(error, abort=self._abort or step.on_fail == "abort"):
                        return
                self._position += 1
            self._stop()
            if not self.clients.confirmed:
                if not self._hold("Run cleanup could not confirm every stop."):
                    return
            completed = True
        except BaseException as exc:
            self._hold(exc, abort=True)
        finally:
            confirmed = self._final_cleanup()
            while (release_errors := self.clients.release()) is False:
                time.sleep(0.05)
            self.release_errors = release_errors
            if release_errors:
                self._persist("event", self.run_id, "release-errors", release_errors)
                self._failure = (
                    self._failure or "Service lease cleanup failed. Check the run events."
                )
                completed = False
            with self._condition:
                self._set(
                    "completed" if completed and confirmed and not self._abort else "aborted",
                    self._failure,
                )
