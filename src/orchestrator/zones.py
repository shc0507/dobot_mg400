"""Durable exclusive zone occupancy, guarded state checks and explicit recovery."""

import copy
import fcntl
import hashlib
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path

from .clients import SERVICES
from .stations import _check, _parent, _read

DEFAULT_CONFIG = Path("/var/lib/mg400/zones/config.toml")
DEFAULT_STATE = Path("/var/lib/mg400/zones/occupancy.json")
CONFIG_SETTING = "MG400_ORCH_ZONES_FILE"
STATE_SETTING = "MG400_ORCH_ZONE_STATE_FILE"
PREDICATES = {"stage_at_load": ("stage",), "robot_clear": ("robot",)}


def zone_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        raise ValueError("A zone name needs 1..64 letters, digits, underscores or hyphens.")
    return value


@dataclass(frozen=True)
class ZonePlan:
    definitions: dict
    required_services: tuple
    state_path: Path
    fingerprint: str


def prepare_zones(recipe, registry, settings=None):
    """Validate configuration and resolve guard leases before any acquisition."""
    settings = os.environ if settings is None else settings
    path = settings.get(CONFIG_SETTING) or DEFAULT_CONFIG
    try:
        with _parent(path) as (parent, name, owner):
            data = _read(parent, name, owner)
    except FileNotFoundError:
        if settings.get(CONFIG_SETTING):
            raise
        data = {"zones": {}}
    if not isinstance(data, dict) or set(data) != {"zones"} or not isinstance(data["zones"], dict):
        raise ValueError("Zone configuration must contain only a zones table.")
    definitions = {}
    for name, spec in data["zones"].items():
        zone_name(name)
        if (
            not isinstance(spec, dict)
            or set(spec) != {"owner", "conditions"}
            or spec["owner"] not in SERVICES
            or not isinstance(spec["conditions"], list)
            or any(
                not isinstance(item, str) or item not in PREDICATES for item in spec["conditions"]
            )
            or len(set(spec["conditions"])) != len(spec["conditions"])
        ):
            raise ValueError(f"Invalid zone definition: {name}")
        if name == "furnace-tube" and (
            spec["owner"] != "stage" or "robot_clear" not in spec["conditions"]
        ):
            raise ValueError("Only stage may own furnace-tube, with robot_clear required.")
        if name == "crucible-load" and (
            spec["owner"] != "robot" or "stage_at_load" not in spec["conditions"]
        ):
            raise ValueError("crucible-load requires robot ownership and stage_at_load.")
        definitions[name] = {"owner": spec["owner"], "conditions": tuple(spec["conditions"])}
    required = set(recipe.services)
    for step in recipe.steps:
        if step.zone is not None:
            zone_name(step.zone)
            if step.zone not in definitions:
                raise ValueError(f"Zone is not configured: {step.zone}")
            if definitions[step.zone]["owner"] not in registry[step.skill].services:
                raise ValueError("A declared zone must belong to a device used by that skill.")
    # Station skills choose zones dynamically. Lease every guard of a configured
    # zone their device can enter, without giving the skill its write methods.
    for spec in definitions.values():
        if spec["owner"] in recipe.services:
            for predicate in spec["conditions"]:
                required.update(PREDICATES[predicate])
    state_path = Path(settings.get(STATE_SETTING) or DEFAULT_STATE)
    fingerprint = hashlib.sha256(json.dumps(definitions, sort_keys=True).encode()).hexdigest()
    try:
        # Inspect existing occupancy even without definitions. Removing a config
        # must never hide an old physical claim.
        claims = OccupancyStore(state_path).snapshot()
    except FileNotFoundError:
        if definitions or settings.get(STATE_SETTING):
            raise
        claims = {}
    if any(
        name not in definitions or claim["config"] != fingerprint for name, claim in claims.items()
    ):
        raise ValueError("Zone configuration changed while durable occupancy remains.")
    return ZonePlan(definitions, tuple(sorted(required)), state_path, fingerprint)


def _process():
    pid = os.getpid()
    return {"pid": pid, "start": _process_start(pid)}


def _process_start(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key in durable zone occupancy.")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"Nonfinite value in durable zone occupancy: {value}")


class OccupancyStore:
    """Atomic storage. Mutation methods compare the recorded claim generation.

    Callers implement authorization and reconciliation; this layer never treats
    file removal or process death as evidence that a physical zone is clear.
    """

    def __init__(self, path):
        self.path = Path(path)

    @staticmethod
    def _load(parent, name, owner):
        try:
            fd = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        except FileNotFoundError:
            return {"version": 1, "claims": {}, "resolutions": []}
        try:
            _check(os.fstat(fd), owner)
            reader = os.open(f"/proc/self/fd/{fd}", os.O_RDONLY | os.O_CLOEXEC)
            with os.fdopen(reader) as source:
                data = json.load(
                    source, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
                )
        finally:
            os.close(fd)
        if (
            not isinstance(data, dict)
            or set(data) != {"version", "claims", "resolutions"}
            or type(data["version"]) is not int
            or data["version"] != 1
            or not isinstance(data["claims"], dict)
            or not isinstance(data["resolutions"], list)
        ):
            raise ValueError("Invalid durable zone occupancy.")
        for zone, claim in data["claims"].items():
            zone_name(zone)
            if (
                not isinstance(claim, dict)
                or set(claim)
                != {"holder", "device", "generation", "services", "config", "process", "status"}
                or not isinstance(claim["holder"], str)
                or not claim["holder"].strip()
                or claim["device"] not in SERVICES
                or not isinstance(claim["generation"], str)
                or not re.fullmatch(r"[a-f0-9]{32}", claim["generation"])
                or not isinstance(claim["services"], list)
                or not claim["services"]
                or any(item not in SERVICES for item in claim["services"])
                or len(set(claim["services"])) != len(claim["services"])
                or claim["device"] not in claim["services"]
                or not isinstance(claim["config"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", claim["config"])
                or not isinstance(claim["process"], dict)
                or set(claim["process"]) != {"pid", "start"}
                or type(claim["process"]["pid"]) is not int
                or claim["process"]["pid"] <= 0
                or not isinstance(claim["process"]["start"], str)
                or not claim["process"]["start"].isdecimal()
                or claim["status"] not in {"occupied", "unknown"}
            ):
                raise ValueError("Invalid durable zone claim.")
        return data

    def _change(self, update=None, *, deadline=None, cancelled=None, commit_guard=None):
        with _parent(self.path) as (parent, name, owner):
            deadline = min(
                time.monotonic() + 0.25, deadline if deadline is not None else float("inf")
            )
            while True:
                if cancelled is not None and cancelled.is_set():
                    raise RuntimeError("Zone storage wait was cancelled.")
                try:
                    fcntl.flock(
                        parent, (fcntl.LOCK_EX if update else fcntl.LOCK_SH) | fcntl.LOCK_NB
                    )
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Zone storage is busy.")
                    if cancelled is None:
                        time.sleep(min(0.005, remaining))
                    else:
                        cancelled.wait(min(0.005, remaining))
            data = self._load(parent, name, owner)
            if update is None:
                return copy.deepcopy(data["claims"])
            result, changed = update(data)
            if not changed:
                return result
            temporary = f".{name}-{uuid.uuid4().hex}"
            fd = os.open(
                temporary,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o640,
                dir_fd=parent,
            )
            try:
                with os.fdopen(fd, "w") as stream:
                    os.fchmod(stream.fileno(), 0o640)
                    _check(os.fstat(stream.fileno()), owner)
                    json.dump(data, stream, allow_nan=False, sort_keys=True)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                with commit_guard() if commit_guard is not None else nullcontext():
                    os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
            return result

    def snapshot(self, *, deadline=None, cancelled=None):
        return self._change(deadline=deadline, cancelled=cancelled)

    def acquire(self, name, claim, *, deadline=None, cancelled=None):
        """Publish a new generation only if no holder already occupies the zone."""

        def change(data):
            if name in data["claims"]:
                return False, False
            conflict = {"crucible-load": "furnace-tube", "furnace-tube": "crucible-load"}.get(name)
            if conflict in data["claims"]:
                return False, False
            data["claims"][name] = copy.deepcopy(claim)
            return True, True

        return self._change(change, deadline=deadline, cancelled=cancelled)

    def unknown(self, holder, expected, *, deadline=None, cancelled=None):
        def change(data):
            changed = False
            for name, generation in expected.items():
                claim = data["claims"].get(name)
                if claim and claim["holder"] == holder and claim["generation"] == generation:
                    changed |= claim["status"] != "unknown"
                    claim["status"] = "unknown"
            return None, changed

        self._change(change, deadline=deadline, cancelled=cancelled)

    def release(
        self, holder, expected, *, resolution=None, deadline=None, cancelled=None, commit_guard=None
    ):
        """Compare all generations before clearing any of them, atomically."""

        def change(data):
            for name, generation in expected.items():
                claim = data["claims"].get(name)
                if not claim or claim["holder"] != holder or claim["generation"] != generation:
                    raise RuntimeError("Zone occupancy changed during release or recovery.")
                if resolution is None and claim["status"] != "occupied":
                    raise RuntimeError(
                        "Unknown occupancy requires audited physical-clearance recovery."
                    )
            for name in expected:
                del data["claims"][name]
            if resolution is not None:
                data["resolutions"].append(copy.deepcopy(resolution))
            return None, bool(expected)

        self._change(change, deadline=deadline, cancelled=cancelled, commit_guard=commit_guard)


def _healthy(service, state, *, idle=False, robot=False):
    if (
        not isinstance(state, dict)
        or service.owned(state) is not True
        or service.healthy(state) is not True
    ):
        raise RuntimeError("A zone service is not healthy or owned by this run.")
    if state.get("locked") is not False or idle and state.get("busy") is not False:
        raise RuntimeError("A zone service is locked or not idle.")
    if "stop_unconfirmed" in state and state["stop_unconfirmed"] is not False:
        raise RuntimeError("A service stop is not confirmed.")
    if "robot_error" in state and state["robot_error"] is not False:
        raise RuntimeError("The robot reports an error.")
    if "fault" in state and state["fault"] is not None:
        raise RuntimeError("A zone service reports a fault.")
    if robot:
        if (
            state.get("mode") != "robot"
            or state.get("robot_error") is not False
            or state.get("stop_unconfirmed") is not False
        ):
            raise RuntimeError("Robot stop is not confirmed.")
    elif "fault" not in state or state["fault"] is not None:
        raise RuntimeError("A zone service has an unknown fault state.")
    _instance(state, robot=robot)


def _instance(state, *, robot=False):
    value = state.get("instance")
    if robot or "instance" in state:
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError("A service instance identifier is missing or invalid.")
    return value


def stage_at_load(state):
    if state.get("feedback_fresh") is not True or state.get("busy") is not False:
        raise RuntimeError("Stage feedback must be fresh and idle at load.")
    motors = state.get("motors")
    if (
        not isinstance(motors, list)
        or len(motors) != 3
        or any(not isinstance(item, dict) for item in motors)
    ):
        raise RuntimeError("Stage homing feedback is unavailable.")
    axes = {item.get("id"): item for item in motors}
    if set(axes) != {"bottom", "top", "insert"} or any(
        item.get("homed") is not True
        or item.get("automation_ready") is not True
        or item.get("mode") != "idle"
        for item in motors
    ):
        raise RuntimeError("Every stage axis must have a fresh, confirmed idle reference.")
    insert = axes["insert"]
    if (
        insert.get("awake") is not True
        or not isinstance(insert.get("at_positions"), list)
        or "load" not in insert["at_positions"]
    ):
        raise RuntimeError("Insert must be held awake at its confirmed named load position.")


class Zones:
    """A run's public zone manager. The durable file also coordinates processes."""

    def __init__(self, plan, services, *, holder, audit):
        self.plan, self.services, self.holder, self.audit = plan, services, holder, audit
        self.store = OccupancyStore(plan.state_path)
        self.process = _process()
        self._lock = threading.RLock()
        self._active = {}
        self._departed = set()
        self._cache = {}
        self.error = None
        self._instances = {}
        self._instance_lock = threading.Lock()
        self._authorities = []
        self._known = set()
        self._recovering = False

    def snapshot(self):
        try:
            self._cache = self.store.snapshot()
        except FileNotFoundError:
            if self.plan.definitions:
                self.error = self.error or "Zone occupancy directory is unavailable."
        except Exception as error:
            self.error = self.error or str(error)
        return copy.deepcopy(self._cache)

    def owned(self):
        return {
            name: claim for name, claim in self.snapshot().items() if claim["holder"] == self.holder
        }

    def _audit(self, kind, data):
        if self.audit(kind, data) is False:
            raise RuntimeError("Cannot record the zone safety event.")

    def bind(self, authority, devices):
        with self._lock:
            if self._recovering:
                raise RuntimeError("A zone recovery is in progress.")
            self._authorities.append(authority)
        return ZoneContext(self, authority, set(devices))

    def retain(self, expected=None):
        expected = expected or {name: claim["generation"] for name, claim in self.owned().items()}
        if expected:
            self.store.unknown(self.holder, expected)
            self.snapshot()

    def fail(self, error):
        """Latch the failure and retain claims without masking the first error."""
        self.error = self.error or str(error)
        try:
            self.retain()
        except Exception:
            # Failed storage cannot authorize entry or clear an existing claim.
            pass

    def check(self, name, authority):
        authority.check()
        spec = self.plan.definitions[name]
        names = {spec["owner"]}
        for condition in spec["conditions"]:
            names.update(PREDICATES[condition])
        states = {}
        for service_name in sorted(names):
            service = self.services[service_name]
            state = authority.call(service.state)
            authority.call(_healthy, service, state, robot=service_name == "robot")
            instance = state.get("instance")
            with self._instance_lock:
                if service_name in self._instances and self._instances[service_name] != instance:
                    raise RuntimeError("A zone service instance changed.")
                self._instances[service_name] = instance
            states[service_name] = state
        for condition in spec["conditions"]:
            if condition == "stage_at_load":
                stage_at_load(states["stage"])
            elif condition == "robot_clear" and "crucible-load" in self.store.snapshot():
                raise RuntimeError("Robot occupancy of crucible-load prevents Insert movement.")
        authority.check()

    def check_held(self, authority):
        claims = self.owned()
        if self.error:
            raise RuntimeError(self.error)
        for name, claim in claims.items():
            if claim["status"] != "occupied" or claim["config"] != self.plan.fingerprint:
                raise RuntimeError("A retained zone requires explicit physical-clearance recovery.")
            self.check(name, authority)

    def before_action(self, device, authority):
        self.check_held(authority)
        claims = self.snapshot()
        if any(
            claim["device"] == device and claim["holder"] != self.holder
            for claim in claims.values()
        ):
            raise RuntimeError("The device has retained occupancy from another run.")
        if device == "stage" and "crucible-load" in claims:
            raise RuntimeError("Stage actuation is blocked while crucible-load is occupied.")
        authority.check()

    @contextmanager
    def guard(self, device, authority):
        """Serialize command dispatch with zone reservations in this run.

        A service lease excludes other runs. This lock also excludes helper
        threads in a multi-device skill; monitors never need this lock.
        """
        authority.check()
        try:
            with self._lock:
                if self._recovering:
                    raise RuntimeError("A zone recovery is in progress.")
                self.before_action(device, authority)
                yield
                self.check_held(authority)
        except BaseException as error:
            self.fail(error)
            raise

    def recover(
        self, holder, services, *, physically_clear=False, note="", live=False, commit_guard=None
    ):
        """Reconcile real public clients before an audited generation-matched clear.

        This never acquires a lease. A dead process's caller must already own all
        recorded services. Live recovery is restricted to this manager's holder.
        """
        with self._lock:
            if self._recovering:
                raise RuntimeError("A zone recovery is already in progress.")
            self._recovering = True
            authorities = tuple(self._authorities)
        try:
            return self._recover(
                holder,
                services,
                physically_clear=physically_clear,
                note=note,
                live=live,
                authorities=authorities,
                commit_guard=commit_guard,
            )
        finally:
            with self._lock:
                self._recovering = False

    def _recover(
        self, holder, services, *, physically_clear, note, live, authorities, commit_guard
    ):
        if not isinstance(note, str) or not note.strip():
            raise ValueError("Supply a nonempty recovery note.")
        try:
            current = self.store.snapshot()
        except FileNotFoundError:
            if self.plan.definitions:
                raise
            current = {}
        claims = {name: claim for name, claim in current.items() if claim["holder"] == holder}
        if claims and physically_clear is not True:
            raise ValueError("Declare physical clearance of every retained zone.")
        if not claims and not live:
            return
        with self._lock:
            if self._active:
                raise RuntimeError("Zone contexts must drain before recovery.")
        if live:
            if holder != self.holder or any(
                claim["process"] != self.process for claim in claims.values()
            ):
                raise RuntimeError("Live recovery is restricted to this run.")
            if any(
                not authority.cancelled.is_set() or not authority.drained
                for authority in self._authorities
            ):
                raise RuntimeError(
                    "Step authority and in-flight operations must drain before recovery."
                )
        elif any(
            _process_start(claim["process"]["pid"]) == claim["process"]["start"]
            for claim in claims.values()
        ):
            raise RuntimeError("The previous process is still alive; recover through its runner.")
        required = {name for claim in claims.values() for name in claim["services"]}
        if not claims:
            required.update(self.plan.required_services)
        if required - set(services):
            raise ValueError("Every retained zone service is required for recovery.")
        instances = {}
        for name in sorted(required):
            service = services[name]
            service.describe()
            state = service.state()
            if service.owned(state) is not True:
                raise RuntimeError(
                    "Recovery requires already-owned services; it will not acquire them."
                )
            instances[name] = _instance(state, robot=name == "robot")
        stop_errors = {}
        for name in sorted(required):
            try:
                if services[name].stop().get("stop") != "confirmed":
                    stop_errors[name] = "stop was not confirmed"
            except Exception as error:
                stop_errors[name] = str(error)
        if stop_errors:
            raise RuntimeError(
                f"Recovery requires fresh confirmed stops from every service: {stop_errors}"
            )
        states = {}
        for name in sorted(required):
            state = services[name].state()
            _healthy(services[name], state, idle=True, robot=name == "robot")
            if state.get("instance") != instances[name]:
                raise RuntimeError("A service instance changed during zone recovery.")
            states[name] = state
        expected = {name: claim["generation"] for name, claim in claims.items()}
        resolution = {
            "holder": holder,
            "zones": expected,
            "note": note,
            "physically_clear": bool(claims),
            "ts_unix": time.time(),
        }
        self._audit(
            "zone-physical-clearance" if claims else "zone-runtime-reconciled",
            {**resolution, "states": states},
        )
        with self._lock:
            if self._active:
                raise RuntimeError("A zone context became active during recovery.")
            if live and (
                tuple(self._authorities) != authorities
                or any(
                    not authority.cancelled.is_set() or not authority.drained
                    for authority in authorities
                )
            ):
                raise RuntimeError("Step authority changed during zone recovery.")
            if expected:
                options = {"commit_guard": commit_guard} if commit_guard is not None else {}
                self.store.release(holder, expected, resolution=resolution, **options)
                self.error = None
            else:
                # No occupancy is erased. Still fence transient-error repair
                # against a run abort or lease failure during reconciliation.
                with commit_guard() if commit_guard is not None else nullcontext():
                    self.error = None
        self.snapshot()


class ZoneContext:
    """Step-bound zone capability. Guard-only services are never exposed here."""

    def __init__(self, manager, authority, devices):
        self.manager, self.authority, self.devices = manager, authority, devices

    @contextmanager
    def take(self, name, *, timeout=None):
        """Release on explicit successful departure; retain exceptional occupancy."""
        try:
            entry = self._enter(name, timeout)
            try:
                yield
            except BaseException:
                self._leave(entry, exceptional=True)
                raise
            else:
                self._leave(entry, exceptional=False)
        except BaseException as error:
            self.manager.fail(error)
            raise

    def declare(self, name):
        """Reserve a recipe-declared zone without assuming return proves departure."""
        try:
            return self._enter(name, None)
        except BaseException as error:
            self.manager.fail(error)
            raise

    def finish(self, entry, *, exceptional=False):
        try:
            self._leave(entry, exceptional=exceptional, declared=True)
        except BaseException as error:
            self.manager.fail(error)
            raise

    def _enter(self, name, timeout):
        manager, authority = self.manager, self.authority
        authority.check()
        zone_name(name)
        if name not in manager.plan.definitions:
            raise ValueError(f"Zone is not configured: {name}")
        spec = manager.plan.definitions[name]
        if spec["owner"] not in self.devices:
            raise RuntimeError("This skill cannot enter another device's zone.")
        deadline = authority.deadline
        if timeout is not None:
            from .recipe import positive

            deadline = min(deadline, time.monotonic() + positive(timeout, "zone timeout"))
        while True:
            authority.check()
            manager.check(name, authority)
            with manager._lock:
                # Recheck after waiting for an in-flight same-run mutation.
                manager.check(name, authority)
                claims = manager.store.snapshot(
                    deadline=authority.deadline, cancelled=authority.cancelled
                )
                claim = claims.get(name)
                if claim is not None and claim["holder"] == manager.holder:
                    if (
                        claim["status"] != "occupied"
                        or claim["config"] != manager.plan.fingerprint
                        or claim["generation"] not in manager._known
                    ):
                        raise RuntimeError(
                            "Retained occupancy requires physical-clearance recovery."
                        )
                elif claim is None:
                    guard_services = {spec["owner"]}
                    for condition in spec["conditions"]:
                        guard_services.update(PREDICATES[condition])
                    claim = {
                        "holder": manager.holder,
                        "device": spec["owner"],
                        "generation": uuid.uuid4().hex,
                        "services": sorted(guard_services),
                        "config": manager.plan.fingerprint,
                        "process": manager.process,
                        "status": "occupied",
                    }
                    if not manager.store.acquire(
                        name, claim, deadline=authority.deadline, cancelled=authority.cancelled
                    ):
                        claim = None
                    else:
                        manager._known.add(claim["generation"])
                else:
                    claim = None
                if claim is not None:
                    entry = (name, claim["generation"])
                    manager._active[entry] = manager._active.get(entry, 0) + 1
                    break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Zone {name} is held by another holder.")
            authority.cancelled.wait(min(0.02, max(0, deadline - time.monotonic())))
        try:
            manager._audit("zone-enter", {"zone": name, "claim": claim})
            manager.check(name, authority)
            manager.snapshot()
        except BaseException:
            self._leave(entry, exceptional=True)
            raise
        return entry

    def _leave(self, entry, *, exceptional, declared=False):
        manager, authority = self.manager, self.authority
        name, generation = entry
        error = None
        if not exceptional:
            try:
                authority.check()
                manager.check(name, authority)
            except BaseException as caught:
                error, exceptional = caught, True
        with manager._lock:
            count = manager._active.get(entry, 0)
            if count <= 0:
                raise RuntimeError("A zone context cannot exit twice.")
            if count == 1:
                del manager._active[entry]
            else:
                manager._active[entry] = count - 1
            if exceptional:
                manager.store.unknown(manager.holder, {name: generation})
                manager._departed.discard(entry)
            elif not declared:
                manager._departed.add(entry)
            if not exceptional and entry not in manager._active and entry in manager._departed:
                manager._audit("zone-departure", {"zone": name, "generation": generation})
                manager.store.release(
                    manager.holder,
                    {name: generation},
                    deadline=authority.deadline,
                    cancelled=authority.cancelled,
                    commit_guard=authority.commit_guard,
                )
                manager._departed.discard(entry)
            manager.snapshot()
        if error is not None:
            raise error
