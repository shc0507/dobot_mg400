"""Zone interlocks, durable occupancy and recovery with fake services and private files."""

import copy
import fcntl
import io
import json
import os
import selectors
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from orchestrator import cli, zones
from orchestrator import zones as recover_module
from orchestrator.clients import RobotAdapter
from orchestrator.recipe import validate
from orchestrator.runner import Runner, StepClient, _Authority
from orchestrator.skills import Registry
from orchestrator.store import RunStore
from orchestrator.zones import OccupancyStore, ZonePlan, Zones, prepare_zones, stage_at_load


@pytest.mark.parametrize("settings", [{}, None])
def test_zone_fixture_isolates_defaults_when_environment_is_removed(
    private_orchestrator_zones, tmp_path, monkeypatch, settings
):
    monkeypatch.delenv(zones.CONFIG_SETTING)
    monkeypatch.delenv(zones.STATE_SETTING)
    assert zones.DEFAULT_CONFIG.is_relative_to(private_orchestrator_zones)
    zones.DEFAULT_CONFIG.write_text('[zones.private]\nowner="robot"\nconditions=[]\n')
    registry = Registry()
    registry.register("work", lambda ctx: None, services=("robot",))
    recipe = zone_recipe(registry, zone="private")
    plan = prepare_zones(recipe, registry, settings=settings)
    assert set(plan.definitions) == {"private"}
    assert plan.state_path.is_relative_to(private_orchestrator_zones)
    assert plan.state_path == zones.DEFAULT_STATE
    assert list(tmp_path.iterdir()) == []


def claim(holder="run-a", generation="a" * 32):
    return {
        "holder": holder,
        "device": "robot",
        "generation": generation,
        "services": ["robot", "stage"],
        "config": "c" * 64,
        "process": {"pid": os.getpid(), "start": zones._process_start(os.getpid())},
        "status": "occupied",
    }


@pytest.fixture
def occupancy():
    with tempfile.TemporaryDirectory(prefix="orc04-occupancy-") as directory:
        root = Path(directory)
        root.chmod(0o750)
        yield OccupancyStore(root / "occupancy.json")


def document(claims):
    return {"version": 1, "claims": claims, "resolutions": []}


def write_document(store, data):
    store.path.write_text(json.dumps(data))
    store.path.chmod(0o640)


def assert_no_temporary_files(store):
    assert sorted(path.name for path in store.path.parent.iterdir()) == ["occupancy.json"]


CHILD_ACQUIRE = r"""
import json, os, sys
from orchestrator.zones import OccupancyStore, _process_start
path, name, holder, generation = sys.argv[1:]
claim = dict(holder=holder, device="robot", generation=generation,
             services=["robot", "stage"], config="c"*64,
             process=dict(pid=os.getpid(), start=_process_start(os.getpid())),
             status="occupied")
print("ready", flush=True)
assert sys.stdin.readline() == "go\n"
result = dict(acquired=OccupancyStore(path).acquire(name, claim), holder=holder)
print(json.dumps(result), flush=True)
"""


def child_environment():
    source = Path(zones.__file__).resolve().parents[1]
    return {**os.environ, "PYTHONPATH": str(source), "PYTHONDONTWRITEBYTECODE": "1"}


def wait_ready(child):
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ)
        assert selector.select(10), "offline subprocess did not become ready"
        assert child.stdout.readline() == "ready\n"


@pytest.mark.parametrize("same_zone", [True, False])
def test_cross_process_acquisition_has_no_double_owner_or_lost_claim(occupancy, same_zone):
    processes = []
    names = ["crucible-load", "crucible-load" if same_zone else "transfer-clear"]
    try:
        for index, name in enumerate(names):
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    CHILD_ACQUIRE,
                    str(occupancy.path),
                    name,
                    f"run-{index}",
                    str(index + 1) * 32,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=child_environment(),
            )
            processes.append(child)
        for child in processes:
            wait_ready(child)
        for child in processes:
            child.stdin.write("go\n")
            child.stdin.flush()
        results = []
        for child in processes:
            output, errors = child.communicate(timeout=10)
            assert child.returncode == 0, errors
            results.append(json.loads(output))
        assert sum(result["acquired"] for result in results) == (1 if same_zone else 2)
        stored = OccupancyStore(occupancy.path).snapshot()
        assert set(stored) == set(names)
        if same_zone:
            winner = next(result["holder"] for result in results if result["acquired"])
            assert stored["crucible-load"]["holder"] == winner
        # Exiting the owning process never authorizes another process to enter.
        assert not OccupancyStore(occupancy.path).acquire("crucible-load", claim("later"))
    finally:
        for child in processes:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


def test_recovery_compares_all_generations_before_clearing_any(occupancy):
    first = claim(generation="a" * 32)
    second = claim(generation="b" * 32)
    replacement = claim(generation="d" * 32)
    assert occupancy.acquire("zone-a", first)
    assert occupancy.acquire("zone-b", second)
    observed = {name: entry["generation"] for name, entry in occupancy.snapshot().items()}
    occupancy.release("run-a", {"zone-b": second["generation"]})
    assert occupancy.acquire("zone-b", replacement)
    before = occupancy.path.read_bytes()
    with pytest.raises(RuntimeError, match="changed"):
        occupancy.release("run-a", observed, resolution={"note": "old recovery observation"})
    assert occupancy.path.read_bytes() == before
    assert set(occupancy.snapshot()) == {"zone-a", "zone-b"}
    assert json.loads(before)["resolutions"] == []
    current = {"zone-a": first["generation"], "zone-b": replacement["generation"]}
    resolution = {"note": "fresh confirmed physical clearance", "holder": "run-a"}
    occupancy.release("run-a", current, resolution=resolution)
    saved = json.loads(occupancy.path.read_text())
    assert saved["claims"] == {} and saved["resolutions"] == [resolution]


def test_unknown_cannot_touch_another_owner_or_a_replacement_generation(occupancy):
    current = claim(generation="b" * 32)
    assert occupancy.acquire("zone-a", current)
    before = occupancy.path.read_bytes()
    occupancy.unknown("other", {"zone-a": current["generation"]})
    occupancy.unknown("run-a", {"zone-a": "a" * 32})
    assert occupancy.path.read_bytes() == before
    occupancy.unknown("run-a", {"zone-a": current["generation"]})
    assert occupancy.snapshot()["zone-a"]["status"] == "unknown"
    assert not occupancy.acquire("zone-a", claim("another"))


@pytest.mark.parametrize("boundary", ["partial-write", "file-fsync", "replace"])
@pytest.mark.parametrize("action", ["acquire", "unknown", "release"])
def test_prepublication_failures_preserve_prior_claims_and_remove_partial_files(
    occupancy, monkeypatch, boundary, action
):
    original_claim = claim()
    assert occupancy.acquire("crucible-load", original_claim)
    before = occupancy.path.read_bytes()
    with monkeypatch.context() as fault:
        if boundary == "partial-write":

            def partial_write(_data, stream, **_kwargs):
                stream.write('{"version":1,"claims":')
                stream.flush()
                raise OSError("injected partial write")

            fault.setattr(zones.json, "dump", partial_write)
        elif boundary == "file-fsync":
            original_fsync = os.fsync

            def file_fsync(fd):
                if stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError("injected file fsync")
                original_fsync(fd)

            fault.setattr(zones.os, "fsync", file_fsync)
        else:

            def replace(*_args, **_kwargs):
                raise OSError("injected replace")

            fault.setattr(zones.os, "replace", replace)
        with pytest.raises(OSError, match="injected"):
            if action == "acquire":
                occupancy.acquire("new-zone", claim("new-run", "b" * 32))
            elif action == "unknown":
                occupancy.unknown("run-a", {"crucible-load": original_claim["generation"]})
            else:
                occupancy.release(
                    "run-a",
                    {"crucible-load": original_claim["generation"]},
                    resolution={"note": "clearance"},
                )
    assert occupancy.path.read_bytes() == before
    assert occupancy.snapshot() == {"crucible-load": original_claim}
    assert_no_temporary_files(occupancy)


@pytest.mark.parametrize("action", ["acquire", "release", "unknown"])
def test_directory_fsync_failure_reports_error_after_atomic_visible_publication(
    occupancy, monkeypatch, action
):
    existing = claim()
    assert occupancy.acquire("crucible-load", existing)
    original_fsync = os.fsync
    with monkeypatch.context() as fault:

        def directory_fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("injected directory fsync")
            original_fsync(fd)

        fault.setattr(zones.os, "fsync", directory_fsync)
        with pytest.raises(OSError, match="directory fsync"):
            if action == "acquire":
                occupancy.acquire("new-zone", claim("new-run", "b" * 32))
            elif action == "release":
                occupancy.release(
                    "run-a",
                    {"crucible-load": existing["generation"]},
                    resolution={"note": "already proven clearance"},
                )
            else:
                occupancy.unknown("run-a", {"crucible-load": existing["generation"]})
    # Rename succeeded: callers must treat the raised error as unknown durability,
    # not as evidence that the previous claim/file is still present.
    saved = json.loads(occupancy.path.read_text())
    if action == "acquire":
        assert set(saved["claims"]) == {"crucible-load", "new-zone"}
        assert not occupancy.acquire("new-zone", claim("competitor"))
    elif action == "release":
        assert saved["claims"] == {}
        assert saved["resolutions"] == [{"note": "already proven clearance"}]
    else:
        assert saved["claims"]["crucible-load"]["status"] == "unknown"
    assert_no_temporary_files(occupancy)


@pytest.mark.parametrize("mode", [0o600, 0o640])
def test_safe_permissions_are_accepted_and_replacement_preserves_owner(occupancy, mode):
    write_document(occupancy, document({"zone-a": claim()}))
    occupancy.path.chmod(mode)
    original = occupancy.path.stat()
    assert occupancy.acquire("zone-b", claim("other", "b" * 32))
    info = occupancy.path.stat()
    assert (info.st_uid, info.st_gid) == (original.st_uid, original.st_gid)
    assert stat.S_IMODE(info.st_mode) == 0o640


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o666, 0o700])
def test_unsafe_file_permissions_refuse_reads_and_mutations(occupancy, mode):
    write_document(occupancy, document({"zone-a": claim()}))
    occupancy.path.chmod(mode)
    before = occupancy.path.read_bytes()
    for operation in (occupancy.snapshot, lambda: occupancy.acquire("zone-b", claim())):
        with pytest.raises(ValueError, match="permissions"):
            operation()
    assert occupancy.path.read_bytes() == before
    assert stat.S_IMODE(occupancy.path.stat().st_mode) == mode


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o777])
def test_unsafe_parent_permissions_are_not_repaired_or_used(occupancy, mode):
    occupancy.path.parent.chmod(mode)
    with pytest.raises(ValueError, match="permissions"):
        occupancy.acquire("zone-a", claim())
    assert not occupancy.path.exists()
    assert stat.S_IMODE(occupancy.path.parent.stat().st_mode) == mode


@pytest.mark.parametrize("field", [4, 5])
def test_owner_or_group_mismatch_refuses_existing_claims(occupancy, monkeypatch, field):
    write_document(occupancy, document({"zone-a": claim()}))
    original = os.fstat

    def wrong_owner(fd):
        info = original(fd)
        if stat.S_ISREG(info.st_mode):
            values = list(info)
            values[field] += 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(zones.os, "fstat", wrong_owner)
    with pytest.raises(ValueError, match="owner"):
        occupancy.snapshot()


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_nonregular_occupancy_is_rejected_before_any_read_open(occupancy, monkeypatch, kind):
    if kind == "symlink":
        target = occupancy.path.parent / "target.json"
        target.write_text("do not read this target")
        target.chmod(0o600)
        occupancy.path.symlink_to(target)
    else:
        os.mkfifo(occupancy.path, 0o600)
    original = os.open

    def inspect_only(path, flags, *args, **kwargs):
        if str(path).startswith("/proc/self/fd/"):
            raise AssertionError("A nonregular inode reached the read-open operation")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(zones.os, "open", inspect_only)
    with pytest.raises(ValueError, match="file types"):
        occupancy.snapshot()


def test_parent_symlink_is_refused_without_following_target(occupancy):
    real = occupancy.path.parent / "real"
    real.mkdir(mode=0o750)
    link = occupancy.path.parent / "alias"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        OccupancyStore(link / "occupancy.json").acquire("zone-a", claim())
    assert not (real / "occupancy.json").exists()


@pytest.mark.parametrize("duplicate", ["claims", "zone"])
def test_duplicate_json_keys_never_discard_an_existing_claim(occupancy, duplicate):
    first, second = json.dumps(claim()), json.dumps(claim("other", "b" * 32))
    if duplicate == "claims":
        data = '{"version":1,"claims":{"zone-a":' + first + '},"claims":{},"resolutions":[]}'
    else:
        data = (
            '{"version":1,"claims":{"zone-a":'
            + first
            + ',"zone-a":'
            + second
            + '},"resolutions":[]}'
        )
    occupancy.path.write_text(data)
    occupancy.path.chmod(0o640)
    with pytest.raises(ValueError):
        occupancy.snapshot()
    assert occupancy.path.read_text() == data


CHILD_LOCK_WAIT = r"""
import json, os, sys, time, threading
from orchestrator.zones import OccupancyStore, _process_start
path, operation, policy = sys.argv[1:]
store = OccupancyStore(path)
started = time.monotonic()
kwargs = {}
if policy == "deadline":
    kwargs["deadline"] = started + 0.04
elif policy == "cancelled":
    cancelled = threading.Event()
    timer = threading.Timer(0.03, cancelled.set)
    timer.start()
    kwargs["cancelled"] = cancelled
claim = dict(holder="other", device="robot", generation="b"*32,
             services=["robot", "stage"], config="c"*64,
             process=dict(pid=os.getpid(), start=_process_start(os.getpid())),
             status="occupied")
try:
    if operation == "snapshot":
        store.snapshot(**kwargs)
    elif operation == "acquire":
        store.acquire("new-zone", claim, **kwargs)
    elif operation == "unknown":
        store.unknown("run-a", {"crucible-load":"a"*32}, **kwargs)
    else:
        store.release("run-a", {"crucible-load":"a"*32}, **kwargs)
except Exception as exc:
    print(json.dumps(dict(error=type(exc).__name__, message=str(exc),
                          elapsed=time.monotonic()-started)), flush=True)
else:
    print(json.dumps(dict(error=None, elapsed=time.monotonic()-started)), flush=True)
"""


@pytest.mark.parametrize("operation", ["snapshot", "acquire", "unknown", "release"])
@pytest.mark.parametrize("policy", ["deadline", "cancelled"])
def test_held_cross_process_lock_obeys_deadline_or_cancellation(occupancy, operation, policy):
    assert occupancy.acquire("crucible-load", claim())
    before = occupancy.path.read_bytes()
    descriptor = os.open(occupancy.path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        result = subprocess.run(
            [sys.executable, "-B", "-c", CHILD_LOCK_WAIT, str(occupancy.path), operation, policy],
            capture_output=True,
            text=True,
            env=child_environment(),
            timeout=3,
        )
        assert result.returncode == 0, result.stderr
        outcome = json.loads(result.stdout)
        assert outcome["error"] == ("TimeoutError" if policy == "deadline" else "RuntimeError")
        assert ("busy" if policy == "deadline" else "cancelled") in outcome["message"]
        assert 0.02 <= outcome["elapsed"] < 1
    finally:
        os.close(descriptor)
    assert occupancy.path.read_bytes() == before
    assert occupancy.snapshot() == {"crucible-load": claim()}


def test_default_lock_wait_is_bounded_without_a_caller_deadline(occupancy):
    descriptor = os.open(occupancy.path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                CHILD_LOCK_WAIT,
                str(occupancy.path),
                "snapshot",
                "default",
            ],
            capture_output=True,
            text=True,
            env=child_environment(),
            timeout=3,
        )
        assert result.returncode == 0, result.stderr
        outcome = json.loads(result.stdout)
        assert outcome["error"] == "TimeoutError"
        assert "busy" in outcome["message"]
        assert 0.15 <= outcome["elapsed"] < 1
    finally:
        os.close(descriptor)
    assert not occupancy.path.exists()


class RecoverMemoryStore:
    def __init__(self, claims):
        self.claims = copy.deepcopy(claims)
        self.releases = []

    def snapshot(self):
        return copy.deepcopy(self.claims)

    def release(self, holder, expected, *, resolution=None, commit_guard=None):
        for name, generation in expected.items():
            value = self.claims.get(name)
            if not value or value["holder"] != holder or value["generation"] != generation:
                raise RuntimeError("Zone occupancy changed during release or recovery.")
        with commit_guard() if commit_guard is not None else nullcontext():
            self.releases.append((holder, copy.deepcopy(expected), copy.deepcopy(resolution)))
            for name in expected:
                del self.claims[name]


class RecoverFakeClient:
    def __init__(self, name, calls):
        self.name, self.calls = name, calls
        self.on_stop = None
        self.on_state = None
        self.stop_result = {"stop": "confirmed"}
        self.state_reads = 0
        self.current = {
            "connected": True,
            "busy": False,
            "locked": False,
            "fault": None,
            "instance": f"{name}-instance",
            "owned": True,
        }
        if name == "robot":
            self.current.update(mode="robot", robot_error=False, stop_unconfirmed=False)

    def acquire(self):
        raise AssertionError("Recovery must never acquire a service lease")

    def describe(self):
        self.calls.append((self.name, "describe"))
        return {"contract": 1}

    def state(self):
        self.calls.append((self.name, "state"))
        self.state_reads += 1
        if self.on_state:
            self.on_state(self)
        return copy.deepcopy(self.current)

    def owned(self, state):
        return state.get("owned") is True

    def healthy(self, state):
        return state.get("connected") is True

    def stop(self):
        self.calls.append((self.name, "stop"))
        if self.on_stop:
            self.on_stop(self)
        if isinstance(self.stop_result, Exception):
            raise self.stop_result
        return copy.deepcopy(self.stop_result)


@pytest.fixture
def recover_env():
    calls, audits = [], []
    clients = {name: RecoverFakeClient(name, calls) for name in ("robot", "stage")}
    plan = ZonePlan(
        {"tray": {"owner": "robot", "conditions": ()}},
        ("robot", "stage"),
        Path("/unused-recovery-probe"),
        "f" * 64,
    )
    manager = Zones(plan, clients, holder="recover-run", audit=lambda *args: audits.append(args))
    manager.store = RecoverMemoryStore(
        {
            "tray": {
                "holder": manager.holder,
                "device": "robot",
                "generation": "a" * 32,
                "services": ["robot", "stage"],
                "config": plan.fingerprint,
                "process": copy.deepcopy(manager.process),
                "status": "unknown",
            },
        }
    )
    authority = _Authority(time.monotonic() + 100)
    authority.revoke()
    manager.bind(authority, {"robot"})
    return SimpleNamespace(
        manager=manager, clients=clients, calls=calls, audits=audits, authority=authority
    )


def recover_call(env, **kwargs):
    options = dict(live=True, physically_clear=True, note="Operator checked physical clearance")
    options.update(kwargs)
    return env.manager.recover(env.manager.holder, env.clients, **options)


@pytest.mark.parametrize(
    "failure", [OSError("fake stop error"), {"stop": "pending"}, {"stop": "unconfirmed"}, None]
)
def test_recover_attempts_all_stops_and_retains_every_claim(recover_env, failure):
    env = recover_env
    env.clients["robot"].stop_result = failure
    with pytest.raises(RuntimeError, match="confirmed stops"):
        recover_call(env)
    assert [name for name, action in env.calls if action == "stop"] == ["robot", "stage"]
    assert env.manager.store.claims and env.manager.store.releases == []
    # A failed attempt must not leave the manager's recovery fence stuck.
    env.manager.bind(_Authority(time.monotonic() + 100), {"robot"})


def test_recover_fences_new_authority_while_reconciling(recover_env):
    env = recover_env

    def bind_during_stop(client):
        env.manager.bind(_Authority(time.monotonic() + 100), {"robot"})

    env.clients["robot"].on_stop = bind_during_stop
    with pytest.raises(RuntimeError, match="recovery is in progress"):
        recover_call(env)
    assert [name for name, action in env.calls if action == "stop"] == ["robot", "stage"]
    assert env.manager.store.claims and not env.manager.store.releases


@pytest.mark.parametrize(
    "change",
    [
        {"instance": "replacement-instance"},
        {"owned": False},
        {"busy": True},
        {"locked": True},
        {"fault": {"code": "unknown", "message": "not reconciled"}},
        {"connected": False},
        {"robot_error": True},
        {"mode": "malformed", "stop_unconfirmed": True},
    ],
)
def test_recover_requires_fresh_healthy_owned_same_instance_after_stop(recover_env, change):
    env = recover_env
    env.clients["robot"].on_stop = lambda client: client.current.update(change)
    with pytest.raises(RuntimeError):
        recover_call(env)
    assert [name for name, action in env.calls if action == "stop"] == ["robot", "stage"]
    assert env.manager.store.claims and not env.manager.store.releases


def test_recover_cannot_clear_a_replacement_claim_generation(recover_env):
    env = recover_env

    def replace_at_audit(kind, data):
        env.manager.store.claims["tray"]["generation"] = "b" * 32
        env.audits.append((kind, data))

    env.manager.audit = replace_at_audit
    with pytest.raises(RuntimeError, match="occupancy changed"):
        recover_call(env)
    assert env.manager.store.claims["tray"]["generation"] == "b" * 32
    assert env.manager.store.releases == []


def test_recover_refuses_prior_process_that_is_still_alive_before_client_calls(recover_env):
    env = recover_env
    # The claim records this exact, currently running Python process.
    with pytest.raises(RuntimeError, match="previous process is still alive"):
        recover_call(env, live=False)
    assert env.calls == [] and env.manager.store.claims


def test_recover_dead_process_uses_already_owned_clients_without_acquiring(
    recover_env, monkeypatch
):
    env = recover_env
    monkeypatch.setattr(recover_module, "_process_start", lambda pid: None)
    recover_call(env, live=False)
    assert not env.manager.store.claims
    assert env.manager.store.releases[0][2]["physically_clear"] is True
    assert [name for name, action in env.calls if action == "stop"] == ["robot", "stage"]


@pytest.mark.parametrize("live", [True, False])
def test_recover_selects_public_confirmation_after_drain_proof(recover_env, monkeypatch, live):
    env = recover_env
    if not live:
        monkeypatch.setattr(recover_module, "_process_start", lambda pid: None)
    confirm = Mock(return_value={"stop": "confirmed"})
    env.clients["robot"].recovery_stop = confirm
    recover_call(env, live=live)
    confirm.assert_called_once_with("robot-instance")
    assert [name for name, action in env.calls if action == "stop"] == ["stage"]
    assert not env.manager.store.claims


@pytest.mark.parametrize("gate", ["clearance", "authority", "process"])
def test_recover_cannot_select_confirmation_before_clearance_or_drain(recover_env, gate):
    env = recover_env
    confirm = Mock(side_effect=AssertionError("The recovery gate must run first"))
    env.clients["robot"].recovery_stop = confirm
    if gate == "authority":
        env.authority.cancelled.clear()
    with pytest.raises((ValueError, RuntimeError)):
        recover_call(env, live=gate != "process", physically_clear=gate != "clearance")
    confirm.assert_not_called()
    assert env.calls == [] and env.manager.store.claims


def test_recover_does_not_acquire_missing_ownership(recover_env):
    env = recover_env
    env.clients["robot"].current["owned"] = False
    with pytest.raises(RuntimeError, match="already-owned"):
        recover_call(env)
    assert not any(action == "stop" for _, action in env.calls)
    assert env.manager.store.claims and not env.manager.store.releases


@pytest.mark.parametrize(
    "options", [{"physically_clear": False}, {"physically_clear": 1}, {"note": ""}, {"note": "  "}]
)
def test_recover_requires_explicit_physical_clearance_and_note_before_client_calls(
    recover_env, options
):
    env = recover_env
    with pytest.raises(ValueError):
        recover_call(env, **options)
    assert env.calls == [] and env.manager.store.claims


def test_recover_rechecks_revoked_authority_before_clear(recover_env):
    env = recover_env

    def alter_authority(client):
        env.authority.cancelled.clear()

    env.clients["robot"].on_stop = alter_authority
    with pytest.raises(RuntimeError, match="authority changed"):
        recover_call(env)
    assert env.manager.store.claims and not env.manager.store.releases


def test_recover_valid_live_clear_records_the_compared_generation(recover_env):
    env = recover_env
    recover_call(env)
    assert not env.manager.store.claims
    holder, generations, resolution = env.manager.store.releases[0]
    assert holder == env.manager.holder and generations == {"tray": "a" * 32}
    assert resolution["zones"] == generations and resolution["note"].strip()
    assert [kind for kind, _ in env.audits] == ["zone-physical-clearance"]


class FakeRobot:
    identity = "review-run"
    owned = RobotAdapter.owned
    healthy = RobotAdapter.healthy

    def state(self):
        return {
            "mode": "robot",
            "controller_id": "review-run",
            "instance": "review-robot",
            "syncing": True,
            "feedback_fresh": True,
            "robot_error": False,
            "busy": False,
            "locked": False,
            "stop_unconfirmed": False,
        }


@pytest.fixture
def zone_context(tmp_path):
    tmp_path.chmod(0o700)
    plan = ZonePlan(
        definitions={"tray": {"owner": "robot", "conditions": ()}},
        required_services=("robot",),
        state_path=tmp_path / "occupancy.json",
        fingerprint="a" * 64,
    )
    manager = Zones(
        plan,
        {"robot": FakeRobot()},
        holder="review-run",
        audit=lambda kind, fields: True,
    )
    authority = _Authority(time.monotonic() + 10)
    return manager, authority, manager.bind(authority, {"robot"})


def test_caught_inner_failure_cannot_be_cleared_by_normal_outer_departure(zone_context):
    manager, _, capability = zone_context

    class InnerFailure(RuntimeError):
        pass

    with pytest.raises(RuntimeError, match="Unknown occupancy"):
        with capability.take("tray"):
            original = manager.store.snapshot()["tray"]
            try:
                with capability.take("tray"):
                    raise InnerFailure("The inner operation has an unknown outcome.")
            except InnerFailure:
                pass
            assert manager.store.snapshot()["tray"]["status"] == "unknown"

    retained = manager.store.snapshot()["tray"]
    assert retained == {**original, "status": "unknown"}


@pytest.mark.parametrize("boundary", ["after_condition_check", "after_temporary_fsync"])
def test_revoked_departure_cannot_remove_durable_claim(zone_context, monkeypatch, boundary):
    manager, authority, capability = zone_context
    context = capability.take("tray")
    context.__enter__()
    original = manager.store.snapshot()["tray"]
    triggered = []

    if boundary == "after_condition_check":
        check = manager.check

        def revoke_after_check(name, current_authority):
            check(name, current_authority)
            current_authority.revoke()
            triggered.append(True)

        monkeypatch.setattr(manager, "check", revoke_after_check)
    else:
        fsync = os.fsync

        def revoke_after_file_sync(fd):
            fsync(fd)
            if stat.S_ISREG(os.fstat(fd).st_mode):
                authority.revoke()
                triggered.append(True)

        monkeypatch.setattr(os, "fsync", revoke_after_file_sync)

    with pytest.raises(RuntimeError):
        context.__exit__(None, None, None)

    assert triggered
    assert authority.cancelled.is_set()
    retained = manager.store.snapshot()["tray"]
    assert retained["holder"] == original["holder"]
    assert retained["generation"] == original["generation"]
    assert retained["status"] in {"occupied", "unknown"}


class ZoneService:
    def __init__(self, name):
        self.name = name
        self.timeout = 0.1
        self.calls = []
        self.fail_heartbeat = False
        self.entered = threading.Event()
        self.release_motion = threading.Event()
        self.block_motion = False
        self.value = {
            "connected": True,
            "feedback_fresh": True,
            "busy": False,
            "locked": False,
            "fault": None,
            "instance": name + "-instance",
            "owned": True,
        }
        if name == "robot":
            self.value.update(mode="robot", robot_error=False, stop_unconfirmed=False)
        else:
            self.value["motors"] = [
                dict(
                    id=name,
                    homed=True,
                    automation_ready=True,
                    mode="idle",
                    awake=True,
                    at_positions=["load"],
                )
                for name in ("bottom", "top", "insert")
            ]

    def describe(self):
        self.calls.append("describe")
        return {"contract": 1}

    def acquire(self):
        self.calls.append("acquire")

    def heartbeat(self):
        if self.fail_heartbeat:
            raise RuntimeError("synthetic lease loss")

    def state(self):
        return copy.deepcopy(self.value)

    def owned(self, state):
        return state.get("owned") is True

    def healthy(self, state):
        return state.get("connected") is True

    def run(self, action, **params):
        self.calls.append(action)
        if self.block_motion and action != "stop":
            self.value["busy"] = True
            self.entered.set()
            assert self.release_motion.wait(5), "offline operation did not drain"
            self.value["busy"] = False
        return self.state()

    command = run

    def stop(self):
        self.calls.append("stop")
        self.release_motion.set()
        self.value["busy"] = False
        return {"stop": "confirmed", "state": self.state()}

    def release(self):
        self.calls.append("release")

    def close(self):
        self.calls.append("close")


@pytest.fixture
def zone_settings(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    config = tmp_path / "zones.toml"
    config.write_text("""[zones.tray]
owner = "robot"
conditions = []
[zones.crucible-load]
owner = "robot"
conditions = ["stage_at_load"]
[zones.furnace-tube]
owner = "stage"
conditions = ["robot_clear"]
""")
    config.chmod(0o640)
    values = {
        "MG400_ORCH_ZONES_FILE": str(config),
        "MG400_ORCH_ZONE_STATE_FILE": str(tmp_path / "occupancy.json"),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def zone_recipe(registry, *, zone="tray", names=("work",), timeout=3):
    return validate(
        {
            "name": "Zone test",
            "version": 1,
            "params": {},
            "steps": [
                {
                    "skill": name,
                    "params": {},
                    "timeout_s": timeout,
                    **({"zone": zone} if zone is not None else {}),
                }
                for name in names
            ],
        },
        registry,
    )


def wait_zone(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "offline zone condition did not settle"
        time.sleep(0.005)


def finish_zone_runner(runner):
    runner.abort()
    runner._thread.join(5)
    assert not runner._thread.is_alive()


def test_guard_service_is_acquired_but_not_exposed_to_robot_skill(zone_settings, tmp_path):
    services = {name: ZoneService(name) for name in ("robot", "stage")}
    registry = Registry()
    observed = []

    def work(ctx):
        assert "acquire" in services["stage"].calls
        assert set(ctx.clients) == {"robot"}
        with pytest.raises(KeyError):
            ctx.clients["stage"]
        with ctx.zones.take("tray"):
            ctx.clients["robot"].run("execute")
        observed.append(True)

    registry.register("work", work, services=("robot",))
    recipe = zone_recipe(registry)
    plan = prepare_zones(recipe, registry, zone_settings)
    assert plan.required_services == ("robot", "stage")
    with RunStore(tmp_path / "runs") as store:
        runner = Runner(recipe, registry, services, store, zone_plan=plan).start()
        try:
            assert runner.wait(5) == "completed", runner.snapshot()
            assert observed == [True]
            assert runner.snapshot()["zones"] == {}
        finally:
            finish_zone_runner(runner)


@pytest.mark.parametrize("failure", ["awake", "lease"])
def test_condition_or_guard_lease_loss_stops_an_inflight_operation_and_retains(
    zone_settings, tmp_path, failure
):
    services = {name: ZoneService(name) for name in ("robot", "stage")}
    services["robot"].block_motion = True
    registry = Registry()
    later = []

    def work(ctx):
        ctx.clients["robot"].run("execute")

    def next_step(ctx):
        later.append(True)

    registry.register("work", work, services=("robot",))
    registry.register("later", next_step)
    recipe = validate(
        {
            "name": "Guard loss",
            "version": 1,
            "params": {},
            "steps": [
                {"skill": "work", "params": {}, "zone": "crucible-load", "timeout_s": 3},
                {"skill": "later", "params": {}, "timeout_s": 1},
            ],
        },
        registry,
    )
    with RunStore(tmp_path / "runs") as store:
        runner = Runner(recipe, registry, services, store).start()
        try:
            assert services["robot"].entered.wait(2)
            if failure == "awake":
                services["stage"].value["motors"][2]["awake"] = False
            else:
                services["stage"].fail_heartbeat = True
            assert runner.wait(3) == "held", runner.snapshot()
            wait_zone(lambda: runner._settled)
            assert later == []
            assert services["robot"].calls.count("execute") == 1
            assert all("stop" in service.calls for service in services.values())
            assert runner.snapshot()["zones"]["crucible-load"]["status"] == "unknown"
        finally:
            finish_zone_runner(runner)


def test_generic_declared_step_return_retains_occupancy(zone_settings, tmp_path):
    registry = Registry()
    registry.register("work", lambda ctx: None, services=("robot",))
    recipe = zone_recipe(registry)
    services = {name: ZoneService(name) for name in ("robot", "stage")}
    with RunStore(tmp_path / "runs") as store:
        runner = Runner(recipe, registry, services, store).start()
        try:
            assert runner.wait(5) == "completed", runner.snapshot()
            assert "tray" in runner.snapshot()["zones"]
        finally:
            finish_zone_runner(runner)
    assert "tray" in OccupancyStore(zone_settings["MG400_ORCH_ZONE_STATE_FILE"]).snapshot()


@pytest.mark.parametrize(
    "field,value",
    [
        ("feedback_fresh", False),
        ("busy", True),
        ("motors", []),
        ("homed", False),
        ("automation_ready", False),
        ("mode", "moving"),
        ("awake", False),
        ("at_positions", []),
        ("at_positions", "load"),
    ],
)
def test_stage_load_condition_requires_authoritative_fresh_named_feedback(field, value):
    state = ZoneService("stage").state()
    if field in {"feedback_fresh", "busy", "motors"}:
        state[field] = value
    else:
        state["motors"][2][field] = value
    with pytest.raises(RuntimeError):
        stage_at_load(state)
    valid = ZoneService("stage").state()
    valid["motors"][2]["position_mm"] = 5  # Named host proof, not an invented zero.
    stage_at_load(valid)


def test_invalid_zone_configuration_fails_before_clients_or_store(
    zone_settings, tmp_path, monkeypatch
):
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(
        'name="No acquisition"\nversion=1\nparams={}\n[[steps]]\n'
        'skill="robot.enable"\nparams={}\ntimeout_s=1\nzone="furnace-tube"\n'
    )
    factories = [
        Mock(side_effect=AssertionError("configuration must fail first")) for _ in range(2)
    ]
    monkeypatch.setattr(cli, "from_settings", factories[0])
    monkeypatch.setattr(cli, "RunStore", factories[1])
    assert cli.main(["run", str(recipe)], out=io.StringIO()) == 2
    for factory in factories:
        factory.assert_not_called()


def test_reserved_zone_conflicts_are_atomic_in_both_directions(occupancy):
    assert occupancy.acquire("crucible-load", claim())
    assert not occupancy.acquire("furnace-tube", claim("run-b", "b" * 32))
    occupancy.release("run-a", {"crucible-load": "a" * 32})
    assert occupancy.acquire("furnace-tube", claim("run-b", "b" * 32))
    assert not occupancy.acquire("crucible-load", claim())


def test_stage_stop_bypasses_occupancy_actuation_guard(zone_context):
    manager, authority, _ = zone_context
    manager.store.acquire("crucible-load", claim("other-run"))
    stage = ZoneService("stage")
    client = StepClient(authority, stage, zones=manager, device="stage")
    with pytest.raises(RuntimeError, match="blocked"):
        client.command("move", motor="insert")
    client.command("stop")
    assert stage.calls == ["stop"]


def test_directory_sync_cannot_block_authority_revocation(zone_context, monkeypatch):
    manager, authority, capability = zone_context
    context = capability.take("tray")
    context.__enter__()
    sync_entered, sync_release = threading.Event(), threading.Event()
    fsync = os.fsync

    def block_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            sync_entered.set()
            assert sync_release.wait(3)
        return fsync(fd)

    monkeypatch.setattr(os, "fsync", block_directory)
    errors = []

    def leave():
        try:
            context.__exit__(None, None, None)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=leave)
    worker.start()
    try:
        assert sync_entered.wait(1)
        revoked = threading.Event()
        revoker = threading.Thread(target=lambda: (authority.revoke(), revoked.set()))
        revoker.start()
        assert revoked.wait(0.5), "directory sync held the authority fence"
        revoker.join(1)
    finally:
        sync_release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert not errors


def test_caught_retention_write_failure_cannot_clear_outer_claim(zone_context, monkeypatch):
    manager, authority, capability = zone_context
    authority._failure = lambda: manager.error
    unknown = manager.store.unknown
    calls = []

    def fail_once(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise OSError("injected retention write failure")
        return unknown(*args, **kwargs)

    monkeypatch.setattr(manager.store, "unknown", fail_once)
    with pytest.raises(RuntimeError):
        with capability.take("tray"):
            try:
                with capability.take("tray"):
                    raise RuntimeError("unknown operation outcome")
            except OSError:
                pass
    assert manager.error
    assert manager.store.snapshot()["tray"]["status"] == "unknown"


@pytest.mark.parametrize("instance", [None, "", 0, False, [], "absent"])
def test_recover_requires_a_valid_reported_instance_identity(recover_env, instance):
    env = recover_env
    if instance == "absent":
        env.clients["robot"].current.pop("instance")
    else:
        env.clients["robot"].current["instance"] = instance
    with pytest.raises(RuntimeError, match="instance"):
        recover_call(env)
    assert env.manager.store.claims and not env.manager.store.releases


def test_recover_accepts_generic_service_without_optional_instance(recover_env):
    env = recover_env
    env.clients["stage"].current.pop("instance")
    recover_call(env)
    assert not env.manager.store.claims and len(env.manager.store.releases) == 1


def recover_held_runner(env):
    # Exercise actual resume/recover with a preconstructed held runner. No worker,
    # renewal thread, or external client is needed for these races.
    import threading

    from orchestrator.runner import Runner

    runner = Runner.__new__(Runner)
    runner._condition = threading.Condition(threading.RLock())
    runner._status, runner._abort = "held", False
    runner._settled, runner._resolving = True, False
    runner._worker = runner._monitor = None
    runner._authority = env.authority
    runner._cancel_run, runner._monitor_stop = threading.Event(), threading.Event()
    runner._resume, runner._position = None, 0
    runner.persistence_error = None
    runner.recipe = SimpleNamespace(steps=[], params={})
    runner.run_id, runner.zones = env.manager.holder, env.manager
    runner._stop = lambda round_id=None, *, recovery=False: 1
    runner._persist = lambda *args, **kwargs: True
    runner.clients = SimpleNamespace(
        services=env.clients,
        reconcile=lambda: {},
        round_done=lambda value: True,
        lifecycle_lock=threading.RLock(),
        error=None,
    )
    return runner


@pytest.mark.parametrize("interruption", ["abort", "lease", "persistence"])
def test_recover_runner_interruption_during_reconciliation_preserves_claim(
    recover_env, interruption
):
    env = recover_env
    runner = recover_held_runner(env)

    def interrupt(client):
        if interruption == "abort":
            runner.abort()
        elif interruption == "lease":
            with runner.clients.lifecycle_lock:
                runner.clients.error = "Lease renewal failed"
        else:
            runner.persistence_error = "Run persistence failed"

    env.clients["robot"].on_stop = interrupt
    with pytest.raises(RuntimeError, match="lost permission to publish"):
        runner.resume(
            manually_completed=True, physically_clear=True, note="Physical clearance checked"
        )
    assert runner._resume is None and not runner._resolving
    if interruption == "abort":
        assert runner._abort and runner._cancel_run.is_set()
    assert env.manager.store.claims and not env.manager.store.releases


@pytest.mark.parametrize("interruption", ["abort", "lease"])
def test_recover_runner_interruption_after_temporary_sync_preserves_durable_claim(
    recover_env, tmp_path, monkeypatch, interruption
):
    import os
    import stat

    from orchestrator.zones import OccupancyStore

    env = recover_env
    tmp_path.chmod(0o700)
    directory = tmp_path / "recovery-publication"
    directory.mkdir(mode=0o700)
    path = directory / "occupancy.json"
    original_claims = env.manager.store.snapshot()
    env.manager.store = OccupancyStore(path)
    assert env.manager.store.acquire("tray", original_claims["tray"])
    original_bytes = path.read_bytes()
    runner = recover_held_runner(env)
    original_sync = os.fsync
    publication_boundaries = []

    def interrupt_after_sync(fd):
        result = original_sync(fd)
        if stat.S_ISREG(os.fstat(fd).st_mode):
            publication_boundaries.append(True)
            if interruption == "abort":
                runner.abort()
            else:
                with runner.clients.lifecycle_lock:
                    runner.clients.error = "Lease renewal failed"
        return result

    monkeypatch.setattr(recover_module.os, "fsync", interrupt_after_sync)
    with pytest.raises(RuntimeError, match="lost permission to publish"):
        runner.resume(
            manually_completed=True, physically_clear=True, note="Physical clearance checked"
        )
    assert publication_boundaries == [True]
    assert runner._resume is None and not runner._resolving
    assert env.manager.store.snapshot() == original_claims
    assert path.read_bytes() == original_bytes
    assert list(directory.iterdir()) == [path]


def test_repaired_unzoned_command_can_resume_with_manual_completion(tmp_path):
    class RepairableRobot(FakeRobot):
        timeout = 0.02

        def __init__(self):
            self.repaired = False
            self.commands = []

        def describe(self):
            return {}

        def acquire(self):
            pass

        def heartbeat(self):
            pass

        def command(self, action, **params):
            self.commands.append(action)
            if not self.repaired:
                raise RuntimeError("A repairable command refusal.")
            return {}

        def stop(self):
            return {"stop": "confirmed"}

        def release(self):
            pass

        def close(self):
            pass

    registry, robot, continued = Registry(), RepairableRobot(), []
    assert "fault" not in robot.state() and "connected" not in robot.state()

    def work(ctx):
        ctx.clients["robot"].command("work")

    def later(ctx):
        continued.append(True)

    registry.register("work", work, services=("robot",))
    registry.register("later", later)
    recipe = validate(
        {
            "name": "Offline manual recovery",
            "version": 1,
            "params": {},
            "steps": [{"skill": name, "params": {}, "timeout_s": 3} for name in ("work", "later")],
        },
        registry,
    )
    plan = ZonePlan({}, ("robot",), tmp_path / "occupancy.json", "a" * 64)
    with RunStore(tmp_path / "runs") as store:
        runner = Runner(recipe, registry, {"robot": robot}, store, zone_plan=plan).start()
        try:
            assert runner.wait(3) == "held"
            deadline = time.monotonic() + 3
            while not runner._settled:
                assert time.monotonic() < deadline, "The offline worker did not drain."
                time.sleep(0.005)
            assert runner.snapshot()["zones"] == {}
            robot.repaired = True
            runner.resume(manually_completed=True, note="Repaired and completed manually.")
            deadline = time.monotonic() + 3
            while runner.status == "held":
                assert time.monotonic() < deadline, runner.snapshot()
                time.sleep(0.005)
            assert runner.wait(3) == "completed", runner.snapshot()
            assert continued == [True]
            assert robot.commands == ["work"]
            assert store.get(runner.run_id)["steps"][0]["status"] == "manually_completed"
            assert runner.snapshot()["zones"] == {}
        finally:
            runner.abort()
            runner._thread.join(4)
            assert not runner._thread.is_alive(), runner.snapshot()


def test_owned_robot_zone_recovery_confirms_manual_disable_without_relocking(tmp_path):
    from orchestrator.rehearsal import Rehearsal
    from orchestrator.skills import default_registry

    registry, calls = default_registry(), []

    def fail(ctx):
        calls.append("failed-step")
        raise RuntimeError("synthetic failure inside the retained zone")

    def later(ctx):
        calls.append("later-step")

    registry.register("test.fail", fail, services=("robot",))
    registry.register("test.later", later)
    recipe = validate(
        {
            "name": "Owned robot zone recovery",
            "version": 1,
            "params": {},
            "steps": [
                {"skill": "robot.enable", "params": {}, "timeout_s": 5},
                {"skill": "test.fail", "params": {}, "timeout_s": 5, "zone": "tray"},
                {"skill": "test.later", "params": {}, "timeout_s": 5},
            ],
        },
        registry,
    )
    plan = ZonePlan(
        {"tray": {"owner": "robot", "conditions": ()}},
        ("robot",),
        tmp_path / "occupancy.json",
        "a" * 64,
    )
    with (
        RunStore(tmp_path / "runs") as store,
        Rehearsal(tmp_path / "demos", recipe.services, identity="orch-zone-demo") as demo,
    ):
        runner = Runner(recipe, registry, demo.clients, store, zone_plan=plan).start()
        try:
            assert runner.wait(10) == "held", runner.snapshot()
            wait_zone(lambda: runner._settled)
            assert runner.snapshot()["zones"]["tray"]["status"] == "unknown"
            raw = demo.robot_state.snapshot()
            assert "fault" not in raw and "connected" not in raw and raw["locked"] is True
            # Explicit operator-equivalent recovery reaches only this owned SIM-01 rig.
            demo.clients["robot"].run("disable", timeout=3)
            assert demo.robot_state.snapshot()["locked"] is False
            runner.resume(
                manually_completed=True,
                physically_clear=True,
                note="Disabled the owned simulator and checked the empty zone.",
            )
            wait_zone(lambda: runner.status == "completed")
            assert calls == ["failed-step", "later-step"]
            assert runner.snapshot()["zones"] == {}
            events = store.get(runner.run_id)["events"]
            resolution = next(
                event for event in events if event["kind"] == "zone-physical-clearance"
            )
            state = json.loads(resolution["data_json"])["states"]["robot"]
            assert state["locked"] is False and state["last_command"]["action"] == "disable"
            assert "fault" not in state and "connected" not in state
        finally:
            finish_zone_runner(runner)


@pytest.mark.parametrize("declaration", ['zone = "missing"', "zone = 1", 'zonez = "tray"'])
def test_invalid_declared_zone_cannot_start_clients_or_demos(
    zone_settings, tmp_path, monkeypatch, declaration
):
    recipe = tmp_path / "invalid-zone.toml"
    recipe.write_text(
        'name="Invalid zone"\nversion=1\nparams={}\n[[steps]]\n'
        'skill="robot.enable"\nparams={}\ntimeout_s=1\n' + declaration + "\n"
    )
    factories = [Mock(side_effect=AssertionError("must validate first")) for _ in range(3)]
    for name, factory in zip(("RunStore", "from_settings", "Rehearsal"), factories):
        monkeypatch.setattr(cli, name, factory)
    assert cli.main(["rehearse", str(recipe)], out=io.StringIO()) == 2
    for factory in factories:
        factory.assert_not_called()


def test_rehearsal_resolves_guard_demo_before_start_and_uses_private_occupancy(
    zone_settings, tmp_path, monkeypatch
):
    recipe = tmp_path / "guard-demo.toml"
    recipe.write_text(
        'name="Guard demo"\nversion=1\nparams={}\n[[steps]]\n'
        'skill="robot.enable"\nparams={}\ntimeout_s=1\nzone="crucible-load"\n'
    )
    production = tmp_path / "production-occupancy.json"
    production.write_bytes(b"must not read or change production occupancy")
    monkeypatch.setenv("MG400_ORCH_ZONE_STATE_FILE", str(production))
    monkeypatch.setenv("MG400_ORCH_ROBOT_URL", "http://production.invalid")
    monkeypatch.setenv("MG400_ORCH_ROBOT_TOKEN", "production-token")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid")
    observed = []

    class Demo:
        def __init__(self, directory, required, *, identity):
            assert required == ("robot", "stage")
            assert (
                not {
                    "MG400_ORCH_ZONE_STATE_FILE",
                    "MG400_ORCH_ROBOT_URL",
                    "MG400_ORCH_ROBOT_TOKEN",
                    "HTTP_PROXY",
                }
                & os.environ.keys()
            )
            self.directory = Path(directory)
            self.clients = {name: ZoneService(name) for name in required}
            observed.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    def runner(recipe, registry, services, store, *, zone_plan, **kwargs):
        assert zone_plan.required_services == ("robot", "stage")
        assert zone_plan.state_path.parent == observed[0].directory
        assert zone_plan.state_path != production
        assert set(services) == {"robot", "stage"}
        return object()

    monkeypatch.setattr(cli, "Rehearsal", Demo)
    monkeypatch.setattr(cli, "Runner", runner)
    monkeypatch.setattr(cli, "_drive", lambda *args: 0)
    assert cli.main(["rehearse", str(recipe), "--runs-dir", str(tmp_path / "runs")]) == 0
    assert production.read_bytes() == b"must not read or change production occupancy"
    assert not observed[0].directory.exists()


class ZonePromptRunner:
    def __init__(self):
        self.status = "created"
        self.resolutions = []
        self.aborted = False
        self.zone = {"tray": {"status": "unknown", "holder": "prompt-run"}}

    def start(self):
        self.status = "held"

    def wait(self, timeout):
        return self.status

    def snapshot(self):
        return {"run_id": "prompt-run", "status": self.status, "zones": copy.deepcopy(self.zone)}

    def resume(self, **kwargs):
        self.resolutions.append(kwargs)
        self.zone = {}
        self.status = "completed"

    def abort(self):
        self.aborted = True
        self.status = "aborted"


def test_physical_clearance_prompt_requires_separate_affirmative_declaration():
    runner = ZonePromptRunner()
    replies = iter(
        [
            "manually-completed",
            "repaired",
            "not clear",
            "manually-completed",
            "verified departure",
            "physically-clear",
        ]
    )
    output = io.StringIO()
    assert cli._drive(runner, output, lambda prompt: next(replies)) == 0
    assert runner.resolutions == [
        {"manually_completed": True, "note": "verified departure", "physically_clear": True}
    ]
    assert "occupancy remains retained" in output.getvalue()


def test_eof_at_physical_clearance_prompt_aborts_without_releasing_occupancy():
    runner = ZonePromptRunner()
    read = Mock(side_effect=["manually-completed", "checked", EOFError()])
    assert cli._drive(runner, io.StringIO(), read) == 1
    assert runner.aborted and not runner.resolutions
    assert runner.zone == {"tray": {"status": "unknown", "holder": "prompt-run"}}


def test_caught_guard_failure_cannot_resume_actuation_after_condition_recovers(zone_context):
    manager, authority, capability = zone_context
    authority._failure = lambda: manager.error
    robot = manager.services["robot"]
    healthy = robot.healthy
    context = capability.take("tray")
    context.__enter__()
    robot.healthy = lambda state: False
    with pytest.raises(RuntimeError):
        with manager.guard("robot", authority):
            pytest.fail("failed condition admitted motion")
    robot.healthy = healthy
    with pytest.raises(RuntimeError):
        with manager.guard("robot", authority):
            pytest.fail("caught failure admitted a later motion")
    with pytest.raises(RuntimeError):
        context.__exit__(None, None, None)
    assert manager.store.snapshot()["tray"]["status"] == "unknown"


def test_foreign_run_occupancy_does_not_request_physical_clearance():
    runner = ZonePromptRunner()
    runner.zone["tray"]["holder"] = "another-run"
    read = Mock(side_effect=["manually-completed", "repaired this run"])
    assert cli._drive(runner, io.StringIO(), read) == 0
    assert read.call_count == 2
    assert runner.resolutions == [{"manually_completed": True, "note": "repaired this run"}]


def test_stage_stop_all_bypasses_only_its_own_known_stop_action(zone_context):
    manager, authority, _ = zone_context
    manager.store.acquire("crucible-load", claim("another-run"))
    stage = ZoneService("stage")
    StepClient(authority, stage, zones=manager, device="stage").command("stop_all")
    assert stage.calls == ["stop_all"]
    robot = ZoneService("robot")
    with pytest.raises(RuntimeError, match="another run"):
        StepClient(authority, robot, zones=manager, device="robot").command("stop_all")
    assert robot.calls == []


def test_stage_dispatch_and_robot_zone_admission_are_serialized(zone_settings, monkeypatch):
    registry = Registry()
    registry.register("work", lambda ctx: None, services=("robot", "stage"))
    recipe = zone_recipe(registry, zone=None)
    services = {name: ZoneService(name) for name in ("robot", "stage")}
    manager = Zones(
        prepare_zones(recipe, registry), services, holder="interleaving", audit=lambda *a: True
    )
    authority = _Authority(time.monotonic() + 5, failure=lambda: manager.error)
    capability = manager.bind(authority, {"robot", "stage"})
    first_check, allow_return = threading.Event(), threading.Event()
    check = manager.check

    def interleave(name, current_authority):
        check(name, current_authority)
        if threading.current_thread().name == "zone-admission" and not first_check.is_set():
            first_check.set()
            assert allow_return.wait(3)

    monkeypatch.setattr(manager, "check", interleave)
    errors, admitted = [], []

    def enter():
        try:
            with capability.take("crucible-load"):
                admitted.append(True)
        except BaseException as error:
            errors.append(error)

    services["stage"].block_motion = True

    def stage_motion():
        try:
            with manager.guard("stage", authority):
                services["stage"].run("move")
        except BaseException as error:
            errors.append(error)

    entrant = threading.Thread(target=enter, name="zone-admission")
    mover = threading.Thread(target=stage_motion)
    entrant.start()
    try:
        assert first_check.wait(1)
        mover.start()
        assert services["stage"].entered.wait(1)
        allow_return.set()
        services["stage"].value["motors"][2]["at_positions"] = []
        services["stage"].release_motion.set()
    finally:
        allow_return.set()
        services["stage"].release_motion.set()
        entrant.join(3)
        if mover.ident is not None:
            mover.join(3)
    assert not entrant.is_alive() and not mover.is_alive()
    assert not admitted and errors
    assert manager.store.snapshot() == {}
    assert services["stage"].calls == ["move"]


def test_monitor_start_failure_never_dispatches_the_skill_and_still_stops(
    zone_settings, tmp_path, monkeypatch
):
    registry = Registry()
    calls = []
    registry.register("work", lambda ctx: calls.append(True), services=("robot",))
    services = {name: ZoneService(name) for name in ("robot", "stage")}
    start = threading.Thread.start

    def fail_monitor(worker):
        if worker.name == "orch-zones":
            raise RuntimeError("injected monitor startup failure")
        return start(worker)

    monkeypatch.setattr(threading.Thread, "start", fail_monitor)
    with RunStore(tmp_path / "runs") as store:
        runner = Runner(zone_recipe(registry), registry, services, store).start()
        try:
            assert runner.wait(3) == "held"
            wait_zone(lambda: runner._settled)
            assert calls == []
            assert all("stop" in service.calls for service in services.values())
        finally:
            finish_zone_runner(runner)
