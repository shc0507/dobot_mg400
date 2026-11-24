"""Exercise durable command admission and cancellation without equipment access."""

import copy
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from labkit.errors import ServiceError
from orchestrator.agent_connect.authority import Authority
from orchestrator.agent_connect.command_store import TERMINAL, CommandStore, canonical
from orchestrator.agent_connect.commands import MAX_BODY_BYTES, CommandHost


class Backend:
    def __init__(self):
        self.revision = 1
        self.available = True
        self.prepared = []
        self.writes = []
        self.stops = []
        self.sent = threading.Event()
        self.release = threading.Event()
        self.stop_entered = threading.Event()
        self.stop_release = threading.Event()
        self.stop_release.set()
        self.block = False
        self.second = False
        self.lose_reply = False
        self.stop_status = "stopped"
        self.prepare_hook = None
        self.write_hook = None
        self.released_pages = []
        self.page_released = threading.Event()

    def release_page(self, page):
        self.released_pages.append(page)
        self.page_released.set()
        return {"outcome": "released", "devices": {"stage": "released"}}

    def catalog(self):
        return [
            {
                "id": "stage.move",
                "label": "Move stage",
                "description": "Move one simulated stage axis.",
                "params": {"type": "object", "properties": {"target": {"type": "number"}}},
                "defaults": {},
                "device": "stage",
                "available": self.available,
                "reason": "" if self.available else "The stage is unavailable.",
                "revision": self.revision,
            }
        ]

    def prepare(self, action, params, preconditions, page):
        self.prepared.append((action, params, preconditions, page))
        if self.prepare_hook:
            self.prepare_hook()
        if set(params) != {"target"}:
            raise ValueError("Send the target parameter.")
        return {"action": action, "params": params, "device": "stage"}

    def _write(self, target):
        self.writes.append(target)
        self.sent.set()
        if self.write_hook:
            self.write_hook()
        if self.lose_reply:
            raise TimeoutError("The device reply was lost.")

    def execute(self, plan, context):
        context.progress(step="moving")
        context.dispatch(self._write, plan["params"]["target"])
        if self.block:
            deadline = time.monotonic() + 3
            while not self.release.wait(0.01):
                context.checkpoint()
                if time.monotonic() >= deadline:
                    raise TimeoutError("The test backend did not finish.")
        context.checkpoint()
        if self.second:
            context.dispatch(self._write, plan["params"]["target"] + 1)
        return {"status": "succeeded", "result": {"target": plan["params"]["target"]}}

    def cancel(self, plan, page):
        self.stops.append((plan, page))
        self.stop_entered.set()
        if not self.stop_release.wait(3):
            raise TimeoutError("The test Stop did not finish.")
        return {"status": self.stop_status}


def connect(authority):
    invitation = authority.create_invitation(authority.human)["invitation"]
    paired = authority.pair(invitation, "Test agent")
    session = authority.connect(paired["credential"])
    return paired, authority.authenticate(session["session_token"])


@pytest.fixture
def rig(tmp_path):
    clock = SimpleNamespace(now=0)
    authority = Authority(clock=lambda: clock.now)
    paired, caller = connect(authority)
    authority.grant(authority.human, caller.id)
    backend = Backend()
    host = CommandHost(lambda: authority, backend, tmp_path / "commands.db")
    state = SimpleNamespace(
        host=host,
        backend=backend,
        authority=authority,
        caller=caller,
        paired=paired,
        clock=clock,
        path=tmp_path / "commands.db",
    )
    yield state
    backend.release.set()
    backend.stop_release.set()
    host.close()
    authority.close()


def body(rig, request_id="request-1", **updates):
    return {
        "request_id": request_id,
        "boot_id": rig.authority.boot_id,
        "control_epoch": rig.authority.control_epoch,
        "contract_id": rig.host.catalog()["contract_id"],
        "action": "stage.move",
        "params": {"target": 4},
        "preconditions": {},
        **updates,
    }


def finished(rig, request_id="request-1", caller=None):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        report = rig.host.get(caller or rig.authority.human, request_id)
        if report["status"] in TERMINAL:
            return report
        time.sleep(0.005)
    pytest.fail("The operation did not reach a terminal status.")


def assert_error(status, function, *args):
    with pytest.raises(ServiceError) as error:
        function(*args)
    assert error.value.status == status


def test_acceptance_is_durable_before_prepare_and_dispatch(rig):
    observed = []

    def durable_state():
        with sqlite3.connect(rig.path) as connection:
            row = connection.execute("SELECT operation_json FROM commands").fetchone()
        observed.append(json.loads(row[0]))

    rig.backend.prepare_hook = durable_state
    rig.backend.write_hook = durable_state
    accepted = rig.host.submit(rig.caller, body(rig))
    assert accepted["status"] == "accepted"
    result = finished(rig)
    assert result["status"] == "succeeded"
    assert [item["status"] for item in observed] == ["accepted", "running"]
    assert [item["dispatch_count"] for item in observed] == [0, 1]
    assert "_plan" not in result
    assert result["result"] == {"target": 4}


def test_concurrent_duplicate_requests_dispatch_once(rig):
    rig.backend.block = True
    request = body(rig)
    with ThreadPoolExecutor(max_workers=8) as executor:
        replies = list(executor.map(lambda _: rig.host.submit(rig.caller, request), range(16)))
    assert len({reply["id"] for reply in replies}) == 1
    assert rig.backend.sent.wait(1)
    assert rig.backend.writes == [4]
    changed = copy.deepcopy(request)
    changed["params"]["target"] = 5
    assert_error(409, rig.host.submit, rig.caller, changed)
    assert_error(409, rig.host.submit, rig.caller, body(rig, "request-2"))
    rig.backend.release.set()
    assert finished(rig)["status"] == "succeeded"


def test_duplicate_after_handover_and_contract_change_returns_record(rig):
    request = body(rig)
    rig.host.submit(rig.caller, request)
    saved = finished(rig)
    rig.authority.take_human(rig.authority.human)
    rig.backend.revision += 1
    assert rig.host.submit(rig.caller, request) == saved
    assert rig.backend.writes == [4]


def test_request_identity_survives_reconnect_but_old_session_is_rejected(rig):
    request = body(rig)
    rig.host.submit(rig.caller, request)
    saved = finished(rig)
    session = rig.authority.connect(rig.paired["credential"])
    reconnected = rig.authority.authenticate(session["session_token"])
    assert_error(401, rig.host.submit, rig.caller, request)
    assert rig.host.submit(reconnected, request) == saved
    assert rig.host.get(reconnected, request["request_id"]) == saved
    assert rig.host.list(reconnected)["count"] == 1
    assert rig.backend.writes == [4]


def test_observer_cannot_submit_or_read_another_clients_history(rig):
    rig.host.submit(rig.caller, body(rig))
    finished(rig)
    _, observer = connect(rig.authority)
    assert_error(403, rig.host.submit, observer, body(rig, "observer-request"))
    assert_error(404, rig.host.get, observer, "request-1")
    assert_error(404, rig.host.cancel, observer, "request-1")
    assert rig.host.list(observer) == {"items": [], "count": 0}
    assert_error(401, rig.host.get, replace(rig.caller), "request-1")


@pytest.mark.parametrize("field", ["boot_id", "contract_id", "control_epoch"])
def test_stale_envelope_is_rejected_before_record_or_device_call(rig, field):
    assert_error(409, rig.host.submit, rig.caller, body(rig, **{field: "old"}))
    assert rig.host.list(rig.caller)["count"] == 0
    assert not rig.backend.prepared


@pytest.mark.parametrize(
    "updates",
    [
        {"extra": True},
        {"request_id": "../unsafe"},
        {"request_id": "a" * 129},
        {"params": []},
        {"preconditions": None},
        {"params": {"target": float("nan")}},
    ],
)
def test_strict_body_rejects_invalid_requests(rig, updates):
    assert_error(400, rig.host.submit, rig.caller, body(rig, **updates))
    assert not rig.backend.writes


def test_large_body_is_rejected_and_omitted_preconditions_are_allowed(rig):
    assert_error(
        413,
        rig.host.submit,
        rig.caller,
        body(rig, params={"target": "x" * MAX_BODY_BYTES}),
    )
    request = body(rig)
    del request["preconditions"]
    rig.host.submit(rig.caller, request)
    assert finished(rig)["status"] == "succeeded"
    assert rig.backend.prepared[0][2] == {}


def test_invalid_parameters_have_durable_rejected_outcome(rig):
    request = body(rig, params={"raw": "do anything"})
    rig.host.submit(rig.caller, request)
    result = finished(rig)
    assert result["status"] == "rejected"
    assert result["dispatch_count"] == 0
    assert rig.host.submit(rig.caller, request) == result


def test_unknown_actions_and_furnace_actions_never_prepare(rig, monkeypatch):
    assert_error(404, rig.host.submit, rig.caller, body(rig, action="dispatch"))
    descriptor = rig.backend.catalog()[0]
    descriptor.update(id="furnace.start", device="furnace", available=True)
    monkeypatch.setattr(rig.backend, "catalog", lambda: [descriptor])
    assert rig.host.catalog()["actions"][0]["available"] is False
    assert_error(403, rig.host.submit, rig.caller, body(rig, action="furnace.start"))
    assert not rig.backend.prepared


def test_availability_does_not_change_digest_but_blocks_new_dispatch(rig):
    initial = rig.host.catalog()["contract_id"]
    rig.backend.available = False
    assert rig.host.catalog()["contract_id"] == initial
    assert_error(409, rig.host.submit, rig.caller, body(rig))
    rig.backend.revision += 1
    assert rig.host.catalog()["contract_id"] != initial


@pytest.mark.parametrize("change", ["handover", "timeout", "contract", "disconnect"])
def test_authority_loss_cancels_without_poll_requests_and_blocks_later_steps(rig, change):
    rig.backend.block = rig.backend.second = True
    rig.host.submit(rig.caller, body(rig))
    assert rig.backend.sent.wait(1)
    if change == "handover":
        rig.authority.take_human(rig.authority.human)
    elif change == "timeout":
        rig.clock.now = 11
    elif change == "contract":
        rig.backend.revision += 1
    else:
        rig.authority.disconnect(rig.caller)
    assert rig.backend.stop_entered.wait(1)
    result = finished(rig)
    assert result["status"] == "stopped"
    assert rig.backend.writes == [4]
    assert len(rig.backend.stops) == 1


@pytest.mark.parametrize("change", ["handover", "timeout", "disconnect", "revoke", "restart"])
def test_idle_agent_page_releases_after_authority_loss(rig, change):
    rig.host.submit(rig.caller, body(rig))
    assert finished(rig)["status"] == "succeeded"
    assert not rig.backend.released_pages
    if change == "handover":
        rig.authority.take_human(rig.authority.human)
    elif change == "timeout":
        rig.clock.now = 11
    elif change == "disconnect":
        rig.authority.disconnect(rig.caller)
    elif change == "revoke":
        rig.authority.revoke(rig.authority.human, rig.caller.client_id)
    else:
        rig.authority.restart()
    assert rig.backend.page_released.wait(1)
    assert rig.backend.released_pages == ["agent-" + rig.caller.id]
    assert not rig.backend.stops


def test_cleanup_waits_for_inflight_stop_and_never_releases_new_agent_page(rig):
    rig.backend.block = True
    rig.backend.stop_release.clear()
    rig.host.submit(rig.caller, body(rig))
    assert rig.backend.sent.wait(1)
    rig.authority.take_human(rig.authority.human)
    assert rig.backend.stop_entered.wait(1)
    _paired, other = connect(rig.authority)
    rig.authority.grant(rig.authority.human, other.id)
    assert not rig.backend.page_released.wait(0.05)
    rig.backend.stop_release.set()
    assert rig.backend.page_released.wait(1)
    assert rig.backend.released_pages == ["agent-" + rig.caller.id]
    assert "agent-" + other.id not in rig.backend.released_pages


def test_cleanup_releases_a_lease_acquired_after_control_loss(rig, monkeypatch):
    acquiring, acquired = threading.Event(), threading.Event()
    lease = {"held": False}

    def execute(plan, context):
        acquiring.set()
        assert acquired.wait(2)
        lease["held"] = True
        context.checkpoint()
        pytest.fail("Lost control must block the device command")

    def release(page):
        assert lease["held"]
        lease["held"] = False
        rig.backend.page_released.set()
        return {"outcome": "released"}

    monkeypatch.setattr(rig.backend, "execute", execute)
    monkeypatch.setattr(rig.backend, "release_page", release)
    rig.host.submit(rig.caller, body(rig))
    assert acquiring.wait(1)
    rig.authority.disconnect(rig.caller)
    try:
        assert rig.backend.stop_entered.wait(1)
        assert not rig.backend.page_released.is_set()
    finally:
        acquired.set()
    assert rig.backend.page_released.wait(1)
    assert lease["held"] is False
    assert not rig.backend.writes


def test_regrant_waits_for_cleanup_of_the_same_session(rig, monkeypatch):
    entered, leave = threading.Event(), threading.Event()

    def release(page):
        entered.set()
        assert leave.wait(2)
        return {"outcome": "unknown", "devices": {"stage": "unconfirmed"}}

    monkeypatch.setattr(rig.backend, "release_page", release)
    rig.host.submit(rig.caller, body(rig))
    assert finished(rig)["status"] == "succeeded"
    rig.authority.take_human(rig.authority.human)
    assert entered.wait(1)
    rig.authority.grant(rig.authority.human, rig.caller.id)
    try:
        assert_error(409, rig.host.submit, rig.caller, body(rig, "next-command"))
    finally:
        leave.set()
    deadline = time.monotonic() + 1
    while rig.host.cleanup_status()["pending"] and time.monotonic() < deadline:
        time.sleep(0.005)
    result = rig.host.cleanup_status()["results"]["agent-" + rig.caller.id]
    assert result["outcome"] == "unknown"


def test_observer_stop_interrupts_without_engine_lock_or_journal(rig):
    rig.backend.block = rig.backend.second = True
    rig.host.submit(rig.caller, body(rig))
    assert rig.backend.sent.wait(1)
    with rig.host.lock, ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(rig.host.interrupt_devices, ["stage"], "Observer requested Stop")
        assert result.result(timeout=0.2) == {"interrupted": ["request-1"]}
    assert rig.backend.stop_entered.wait(1)
    assert finished(rig)["status"] == "stopped"
    assert rig.backend.writes == [4]


def test_stop_on_another_device_does_not_cancel_stage_sequence(rig):
    rig.backend.block = True
    rig.host.submit(rig.caller, body(rig))
    assert rig.backend.sent.wait(1)
    assert rig.host.interrupt_devices(["robot"]) == {"interrupted": []}
    rig.backend.release.set()
    assert finished(rig)["status"] == "succeeded"
    assert not rig.backend.stops


@pytest.mark.parametrize("target", ["stage", "all_motion"])
def test_stop_during_admission_blocks_later_dispatch(rig, monkeypatch, target):
    entered, leave = threading.Event(), threading.Event()
    insert = rig.host._store.insert

    def blocked_insert(*args):
        entered.set()
        assert leave.wait(2)
        return insert(*args)

    monkeypatch.setattr(rig.host._store, "insert", blocked_insert)
    request = body(rig)
    with ThreadPoolExecutor(max_workers=2) as executor:
        submission = executor.submit(rig.host.submit, rig.caller, request)
        assert entered.wait(1)
        try:
            interrupted = executor.submit(rig.host.interrupt_devices, target)
            assert interrupted.result(timeout=0.2) == {"interrupted": []}
        finally:
            leave.set()
        submission.result(timeout=1)
    assert finished(rig)["status"] == "stopped"
    assert not rig.backend.prepared
    assert not rig.backend.writes
    assert not rig.backend.stops


def test_cancel_remains_nonblocking_after_operator_loss_and_sends_one_stop(rig):
    rig.backend.block = True
    rig.backend.stop_release.clear()
    rig.host.submit(rig.caller, body(rig))
    assert rig.backend.sent.wait(1)
    rig.authority.take_human(rig.authority.human)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(rig.host.cancel, rig.caller, "request-1")
        assert future.result(timeout=0.5)["status"] == "cancelling"
    assert rig.backend.stop_entered.wait(1)
    rig.host.cancel(rig.caller, "request-1")
    assert rig.backend.stops[0][1] == "agent-" + rig.caller.id
    assert len(rig.backend.stops) == 1
    rig.backend.stop_release.set()
    assert finished(rig)["status"] == "stopped"


def test_unconfirmed_stop_remains_unknown(rig):
    rig.backend.block = True
    rig.backend.stop_status = "unknown"
    rig.host.submit(rig.caller, body(rig))
    assert rig.backend.sent.wait(1)
    rig.host.cancel(rig.authority.human, "request-1")
    assert finished(rig)["status"] == "unknown"


def test_lost_reply_remains_unknown_even_after_stop_and_never_replays(rig):
    rig.backend.lose_reply = True
    request = body(rig)
    rig.host.submit(rig.caller, request)
    result = finished(rig)
    assert result["status"] == "unknown"
    assert result["dispatch_count"] == 1
    assert "lost" in result["error"]
    assert rig.host.submit(rig.caller, request) == result
    assert rig.backend.writes == [4]


def test_returned_unknown_stops_before_releasing_the_active_slot(rig, monkeypatch):
    def execute(plan, context):
        context.dispatch(rig.backend._write, plan["params"]["target"])
        return {"status": "unknown", "error": "The service returned no completion ID."}

    monkeypatch.setattr(rig.backend, "execute", execute)
    rig.backend.stop_release.clear()
    request = body(rig)
    rig.host.submit(rig.caller, request)
    assert rig.backend.stop_entered.wait(1)
    assert_error(409, rig.host.submit, rig.caller, body(rig, "request-2"))
    rig.backend.stop_release.set()
    saved = finished(rig)
    assert saved["status"] == "unknown"
    assert len(rig.backend.stops) == 1
    assert rig.host.submit(rig.caller, request) == saved
    assert rig.backend.writes == [4]


def test_prepare_does_not_hold_admission_or_stop_locks(rig):
    entered, release = threading.Event(), threading.Event()

    def prepare():
        entered.set()
        assert release.wait(2)

    rig.backend.prepare_hook = prepare
    try:
        rig.host.submit(rig.caller, body(rig))
        assert entered.wait(1)
        with ThreadPoolExecutor(max_workers=1) as executor:
            response = executor.submit(rig.host.cancel, rig.caller, "request-1").result(0.5)
        assert response["status"] == "cancelling"
    finally:
        release.set()
    assert finished(rig)["status"] == "stopped"
    assert not rig.backend.writes


@pytest.mark.parametrize("previous", ["accepted", "running", "cancelling"])
def test_restart_marks_unfinished_history_unknown_and_never_replays(rig, previous):
    request = body(rig)
    rig.host.submit(rig.caller, request)
    saved = finished(rig)
    rig.host.close()
    saved["status"] = previous
    with sqlite3.connect(rig.path) as connection:
        connection.execute("UPDATE commands SET operation_json=?", (canonical(saved),))
    restarted = CommandHost(lambda: rig.authority, rig.backend, rig.path)
    try:
        rig.authority.restart()
        session = rig.authority.connect(rig.paired["credential"])
        caller = rig.authority.authenticate(session["session_token"])
        assert restarted.get(caller, "request-1")["status"] == "unknown"
        assert restarted.submit(caller, request)["status"] == "unknown"
        assert rig.backend.writes == [4]
    finally:
        restarted.close()


@pytest.mark.parametrize("corruption", ["format", "record", "version"])
def test_unreadable_history_fails_closed_without_replacing_data(rig, corruption):
    request = body(rig)
    rig.host.submit(rig.caller, request)
    finished(rig)
    rig.host.close()
    if corruption == "format":
        rig.path.write_bytes(b"This is not a command database.")
    else:
        with sqlite3.connect(rig.path) as connection:
            sql = (
                "UPDATE commands SET operation_json='{}'"
                if corruption == "record"
                else "UPDATE command_meta SET value='999' WHERE key='version'"
            )
            connection.execute(sql)
    before = rig.path.read_bytes()
    unavailable = CommandHost(lambda: rig.authority, rig.backend, rig.path)
    try:
        assert_error(503, unavailable.submit, rig.caller, body(rig, "new-request"))
        assert_error(503, unavailable.list, rig.caller)
        assert rig.path.read_bytes() == before
        assert rig.backend.writes == [4]
    finally:
        unavailable.close()


def test_failed_dispatch_intent_write_sends_no_command(rig, monkeypatch):
    original = rig.host._store.save

    def fail_dispatch(record):
        if record["dispatch_count"]:
            raise sqlite3.OperationalError("The disk is full.")
        return original(record)

    monkeypatch.setattr(rig.host._store, "save", fail_dispatch)
    rig.host.submit(rig.caller, body(rig))
    assert rig.backend.stop_entered.wait(1)
    assert_error(503, rig.host.submit, rig.caller, body(rig, "new-request"))
    assert not rig.backend.writes


def test_storage_failure_cannot_block_active_cancellation(rig, monkeypatch):
    rig.backend.block = True
    rig.host.submit(rig.caller, body(rig))
    assert rig.backend.sent.wait(1)

    def unavailable(_record):
        raise sqlite3.OperationalError("The database is unavailable.")

    monkeypatch.setattr(rig.host._store, "save", unavailable)
    assert rig.host.cancel(rig.caller, "request-1")["status"] == "cancelling"
    assert rig.backend.stop_entered.wait(1)
    assert_error(503, rig.host.submit, rig.caller, body(rig, "new-request"))


def test_locked_database_does_not_delay_device_stop(rig):
    rig.backend.block = True
    rig.host.submit(rig.caller, body(rig))
    assert rig.backend.sent.wait(1)
    with sqlite3.connect(rig.path) as other:
        other.execute("BEGIN EXCLUSIVE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            response = executor.submit(rig.host.cancel, rig.caller, "request-1")
            assert rig.backend.stop_entered.wait(0.2)
            assert response.result(0.5)["status"] == "cancelling"
        other.rollback()
    assert_error(503, rig.host.submit, rig.caller, body(rig, "new-request"))


def test_publish_failure_does_not_change_the_operation(rig):
    def publish(_kind, _payload):
        raise RuntimeError("No subscriber is connected.")

    rig.host.publish = publish
    rig.host.submit(rig.caller, body(rig))
    assert finished(rig)["status"] == "succeeded"


def test_history_lists_bounded_summaries_and_rejects_ambiguous_human_ids(rig):
    request = body(rig)
    rig.host.submit(rig.caller, request)
    first = finished(rig)
    for index in range(55):
        record = dict(first, request_id=f"saved-{index}", params={"secret": "large profile"})
        rig.host._store.insert(dict(request, request_id=record["request_id"]), record)
    report = rig.host.list(rig.caller)
    assert report["count"] == 56
    assert len(report["items"]) == 50
    assert report["items"][0]["request_id"] == "saved-54"
    assert all("params" not in item and "result" not in item for item in report["items"])
    assert report["items"][0]["progress"] == {"step": "moving"}
    _, other = connect(rig.authority)
    rig.authority.grant(rig.authority.human, other.id)
    rig.host.submit(other, body(rig))
    deadline = time.monotonic() + 2
    while rig.host.get(other, "request-1")["status"] not in TERMINAL:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    assert_error(409, rig.host.get, rig.authority.human, "request-1")
    assert_error(409, rig.host.cancel, rig.authority.human, "request-1")
    assert rig.host.get(rig.caller, "request-1")["id"] == first["id"]


def test_close_joins_poll_execute_and_stop_threads(rig, monkeypatch):
    closed = []
    monkeypatch.setattr(rig.backend, "close", lambda: closed.append(True), raising=False)
    rig.backend.block = True
    rig.host.submit(rig.caller, body(rig))
    assert rig.backend.sent.wait(1)
    rig.host.close()
    assert not rig.host._poller.is_alive()
    assert not any(thread.is_alive() for thread in rig.host._threads)
    assert len(rig.backend.stops) == 1
    assert_error(503, rig.host.submit, rig.caller, body(rig, "new-request"))
    rig.host.close()
    assert closed == [True]


def test_store_rejects_symlink_before_creating_database(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("This account cannot create directory links.")
    with pytest.raises(ValueError, match="symbolic"):
        CommandStore(link / "commands.db")
    assert not (target / "commands.db").exists()
