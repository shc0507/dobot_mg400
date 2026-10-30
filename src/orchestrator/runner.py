"""Sequential recipes with permanently revoked failed-step contexts."""

import copy
import logging
import threading
import time
from contextlib import contextmanager
from types import MappingProxyType

from .clients import SERVICES, Clients
from .recipe import positive
from .zones import PREDICATES, Zones, prepare_zones, zone_name


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

    @contextmanager
    def commit_guard(self):
        """Fence a bounded local publication against revocation and lease loss.

        Prepare data and acquire the zone/storage locks before entering. Do not
        perform service calls or wait for another lock while this fence is held.
        """
        with self._lock:
            self.check()
            yield


class StepClient:
    """Expose guarded operations only, never the underlying service client."""

    __slots__ = ("__call", "__remaining")

    def __init__(self, authority, service, *, zones=None, device=None):
        def invoke(method, *args, **kwargs):
            if (
                zones is not None
                and method in {"run", "command"}
                and args[0] != "stop"
                and not (device == "stage" and args[0] == "stop_all")
            ):

                def guarded():
                    with zones.guard(device, authority):
                        return getattr(service, method)(*args, **kwargs)

                return authority.call(guarded)
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


class Context:
    def __init__(self, authority, services, store, run_id, position, chip_id, time_scale, zones):
        self.clients = MappingProxyType(
            {
                name: StepClient(authority, service, zones=zones, device=name)
                for name, service in services.items()
            }
        )
        self.store = StepStore(authority, store, run_id, position, chip_id)
        self.zones = zones.bind(authority, services)
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

    def __init__(self, recipe, registry, services, store, *, time_scale=1, zone_plan=None):
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
        self.zone_plan = copy.deepcopy(zone_plan or prepare_zones(recipe, registry))
        definitions = self.zone_plan.definitions
        if not isinstance(definitions, dict):
            raise ValueError("Zone definitions must be a mapping.")
        guard_services = set(required)
        for name, spec in definitions.items():
            zone_name(name)
            if (
                not isinstance(spec, dict)
                or set(spec) != {"owner", "conditions"}
                or not isinstance(spec["owner"], str)
                or spec["owner"] not in SERVICES
                or not isinstance(spec["conditions"], (list, tuple))
                or any(
                    not isinstance(item, str) or item not in PREDICATES
                    for item in spec["conditions"]
                )
                or len(set(spec["conditions"])) != len(spec["conditions"])
            ):
                raise ValueError(f"Invalid zone definition: {name}")
            if name == "crucible-load" and (
                spec["owner"] != "robot" or "stage_at_load" not in spec["conditions"]
            ):
                raise ValueError("crucible-load requires robot ownership and stage_at_load.")
            if name == "furnace-tube" and (
                spec["owner"] != "stage" or "robot_clear" not in spec["conditions"]
            ):
                raise ValueError("Only stage may own furnace-tube, with robot_clear required.")
            if spec["owner"] in required:
                for predicate in spec["conditions"]:
                    guard_services.update(PREDICATES[predicate])
        for step in recipe.steps:
            if step.zone is not None:
                zone_name(step.zone)
                if step.zone not in definitions:
                    raise ValueError(f"Zone is not configured: {step.zone}")
                if definitions[step.zone]["owner"] not in registry[step.skill].services:
                    raise ValueError("A declared zone must belong to a device used by that skill.")
        if (
            not isinstance(self.zone_plan.required_services, (tuple, list))
            or any(not isinstance(name, str) for name in self.zone_plan.required_services)
            or set(self.zone_plan.required_services) != guard_services
        ):
            raise ValueError("Zone services must match the recipe and all required guards.")
        required.update(self.zone_plan.required_services)
        if required - set(services):
            raise ValueError("A required service is not configured.")
        self.clients = Clients({name: services[name] for name in sorted(required)})
        self.run_id = store.create(recipe)
        self._condition = threading.Condition(threading.RLock())
        self._status = "created"
        self._abort = False
        self._cancel_run = threading.Event()
        self._resume = None
        self._worker = self._thread = self._authority = None
        self._monitor = None
        self._monitor_stop = threading.Event()
        self._position = 0
        self._settled = False
        self._resolving = False
        self._final_round = None
        self._failure = ""
        self.persistence_error = None
        self.release_errors = {}
        self.zones = Zones(
            self.zone_plan,
            self.clients.services,
            holder=self.run_id,
            audit=lambda kind, data: self._persist(
                "event",
                self.run_id,
                kind,
                data,
                chip_id=self._chip_id(),
                step=self._position,
            ),
        )

    @property
    def status(self):
        with self._condition:
            return self._status

    def snapshot(self):
        """Report safety outcomes even when the persistent audit store failed."""
        # File inspection may wait briefly. Never take a zone/storage lock while
        # holding the runner condition or client lifecycle lock.
        zones = self.zones.snapshot()
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
                "zones": zones,
                "zone_error": self.zones.error,
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
            with self._condition, self.clients.lifecycle_lock:
                self.persistence_error = self.persistence_error or str(exc)
                self._failure = f"Run persistence failed: {self.persistence_error}"
                self._cancel()
                self._monitor_stop.set()
                self._condition.notify_all()
            try:
                logging.getLogger(__name__).error(self._failure)
            except Exception:
                pass
            return False

    def _cancel(self):
        # Callers hold the runner condition. Publication of cancellation shares
        # the lifecycle fence used by final physical-clearance publication.
        with self.clients.lifecycle_lock:
            self._abort = True
            self._cancel_run.set()
            if self._authority is not None:
                self._authority.revoke()

    @contextmanager
    def _recovery_commit_guard(self):
        # Zones already holds its manager/storage locks. Only the atomic local
        # publication belongs inside this fence; no condition or service waits.
        with self.clients.lifecycle_lock:
            if (
                self._abort
                or self._cancel_run.is_set()
                or self.clients.error
                or self.persistence_error
            ):
                raise RuntimeError("The run lost permission to publish zone recovery.")
            yield

    def start(self):
        with self._condition:
            if self._thread is not None or self._status != "created":
                raise RuntimeError("A run can start only once.")
            try:
                self._thread = threading.Thread(target=self._run, name="orch-run", daemon=True)
                self._thread.start()
            except Exception as exc:
                self._cancel()
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
            self._cancel()
            self._monitor_stop.set()
            self._condition.notify_all()
            return True

    def resume(self, *, manually_completed=False, note="", physically_clear=False):
        with self._condition:
            if self._status != "held" or self._abort:
                raise RuntimeError("Only a held run can resume.")
            if manually_completed is not True or not isinstance(note, str) or not note.strip():
                raise ValueError("Confirm manual completion and supply a resolution note.")
            if not self._settled or not self._drained() or self._resolving:
                raise RuntimeError("The failed worker and final stop must finish before recovery.")
            self._resolving = True
            self._settled = False
        # Manual recovery can itself change the service state. Always send a
        # fresh stop after the declaration; cached confirmation is insufficient.
        round_id = None
        try:
            round_id = self._stop()
            states = self.clients.reconcile()
            claims = self.zones.owned()
            if claims and physically_clear is not True:
                raise ValueError("Declare physical clearance of retained zones before resuming.")
            if claims or self.zones.error:
                self.zones.recover(
                    self.run_id,
                    self.clients.services,
                    physically_clear=physically_clear,
                    note=note,
                    live=True,
                    commit_guard=self._recovery_commit_guard,
                )
            if self.zones.error:
                raise RuntimeError(f"Zone recovery is incomplete: {self.zones.error}")
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
        return (
            (self._worker is None or not self._worker.is_alive())
            and (self._monitor is None or not self._monitor.is_alive())
            and (self._authority is None or self._authority.drained)
        )

    def _retain_zones(self):
        """Keep uncertainty durable without delaying or bypassing stop cleanup."""
        try:
            self.zones.retain()
        except Exception as exc:
            self.zones.error = self.zones.error or str(exc)
            with self._condition:
                self._failure = f"{self._failure}; zone occupancy persistence failed: {exc}".lstrip(
                    "; "
                )
            self._persist("event", self.run_id, "zone-retain-failed", {"error": str(exc)})

    def _stop(self, round_id=None):
        result = self.clients.stop_all(round_id)
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
            self._monitor_stop.set()
            self._failure = str(error)
            if abort:
                self._cancel()
            self._settled = False
        was_drained = self._drained()
        initial_round = self._stop()
        self._retain_zones()
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
                done = self.clients.round_done(final_round)
                if done:
                    # A previously registered operation may have published a
                    # claim after initial revocation. Preserve that claim too.
                    self._retain_zones()
                with self._condition:
                    self._final_round = final_round
                    self._settled = done
            note = None
            with self._condition:
                if self._abort:
                    if self._status != "aborting":
                        self._set("aborting", self._failure)
                    if self._settled and not self._resolving:
                        return False
                elif self._resume is not None and not self._resolving:
                    note, self._resume = self._resume, None
                if note is None:
                    self._condition.wait(0.05)
            if note is not None:
                # Recheck outside the runner condition: abort remains available
                # while service or occupancy reads are in flight.
                try:
                    self.clients.reconcile()
                    claims = self.zones.owned()
                    if self.zones.error or claims:
                        raise RuntimeError("Retained zone occupancy still requires recovery.")
                except Exception as exc:
                    with self._condition:
                        self._failure = str(exc)
                        self._set("held", self._failure)
                else:
                    with self._condition:
                        if self._abort:
                            continue
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

    def _step(self, step):
        with self._condition, self.clients.lifecycle_lock:
            if self._abort or self.clients.error or self.zones.error:
                return RuntimeError(
                    self.clients.error or self.zones.error or "A person aborted the run."
                )
            self._authority = _Authority(
                time.monotonic() + step.timeout_s,
                failure=lambda: self.clients.error or self.zones.error,
                lifecycle_lock=self.clients.lifecycle_lock,
            )
            services = {
                name: self.clients.services[name] for name in self.registry[step.skill].services
            }
            authority = self._authority
            self._monitor_stop = threading.Event()
            monitor_stop = self._monitor_stop
        # Zone binding takes its manager lock. The lock order is manager then
        # lifecycle, so binding must be outside the runner/lifecycle fence.
        try:
            ctx = Context(
                self._authority,
                services,
                self.store,
                self.run_id,
                self._position,
                self._chip_id(),
                self.time_scale,
                self.zones,
            )
        except BaseException as exc:
            authority.revoke()
            return exc
        outcome = {}

        def invoke():
            entry = None
            try:
                if step.zone is not None:
                    entry = ctx.zones.declare(step.zone)
                outcome["result"] = self.registry[step.skill].function(
                    ctx, **copy.deepcopy(step.params)
                )
                self.zones.check_held(authority)
                ctx.check()
                if entry is not None:
                    finished, entry = entry, None
                    ctx.zones.finish(finished)
            except BaseException as exc:
                outcome["error"] = exc
                if entry is not None:
                    try:
                        ctx.zones.finish(entry, exceptional=True)
                    except BaseException as cleanup:
                        outcome["error"] = RuntimeError(f"{exc}; zone retention failed: {cleanup}")

        def monitor():
            try:
                while not monitor_stop.is_set():
                    self.zones.check_held(authority)
                    if monitor_stop.is_set():
                        return
                    authority.check()
                    monitor_stop.wait(0.05)
            except BaseException as exc:
                # Revocation is immediate and does not wait for the skill's
                # zone guard or its possibly blocked device operation.
                authority.revoke()
                outcome["guard_error"] = exc
                monitor_stop.set()

        def failed(error):
            authority.revoke()
            monitor_stop.set()
            return error

        with self._condition, self.clients.lifecycle_lock:
            if self._abort or self.clients.error or self.zones.error:
                return failed(
                    RuntimeError(
                        self.clients.error or self.zones.error or "A person aborted the run."
                    )
                )
            if not self._persist("step", self.run_id, self._position, "running"):
                return failed(RuntimeError("Cannot record step startup."))
            try:
                self._monitor = threading.Thread(target=monitor, name="orch-zones", daemon=True)
                self._monitor.start()
                self._worker = threading.Thread(target=invoke, name="orch-skill", daemon=True)
                self._worker.start()
            except Exception as exc:
                return failed(exc)
        while self._worker.is_alive():
            if self._abort:
                return failed(RuntimeError("A person aborted the run."))
            if self.clients.error or self.zones.error:
                return failed(RuntimeError(self.clients.error or self.zones.error))
            if "guard_error" in outcome:
                return failed(outcome["guard_error"])
            if time.monotonic() >= authority.deadline:
                return failed(TimeoutError("The step timed out. It will not be retried."))
            self._worker.join(0.02)
        monitor_stop.set()
        while self._monitor.is_alive():
            if self._abort or self.clients.error or self.zones.error:
                return failed(
                    RuntimeError(
                        self.clients.error or self.zones.error or "A person aborted the run."
                    )
                )
            if time.monotonic() >= authority.deadline:
                return failed(TimeoutError("Zone feedback did not drain before the step deadline."))
            self._monitor.join(0.02)
        authority.revoke()
        if not authority.drained:
            return RuntimeError("The skill returned with service operations still in flight.")
        if self._abort or self.clients.error or self.zones.error:
            return RuntimeError(
                self.clients.error or self.zones.error or "A person aborted the run."
            )
        if "guard_error" in outcome:
            return outcome["guard_error"]
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
            self._monitor_stop.set()
        drained = self._drained()
        round_id = self._stop()
        while not self._drained():
            time.sleep(0.02)
        if not drained:
            round_id = self._stop()
        while not self.clients.round_done(round_id):
            self._stop(round_id)
            time.sleep(0.02)
        self._retain_zones()
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
                self.zones.snapshot()
                if self.zones.error:
                    raise RuntimeError(self.zones.error)
                self.clients.acquire(self.zone_plan.required_services, cancelled=self._cancel_run)
                self.zones.services = self.clients.services
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
                    "completed"
                    if completed and confirmed and not self._abort and not self.zones.error
                    else "aborted",
                    self._failure,
                )
