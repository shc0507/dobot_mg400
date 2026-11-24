"""Stops dispatch independently of grants, audit storage and other Stop targets."""

import sqlite3
import threading
import time

import pytest

from labkit.errors import ServiceError
from orchestrator.agent_connect import stops
from orchestrator.agent_connect.stop_store import StopStore

OBSERVER = {
    "kind": "agent",
    "id": "agent-a",
    "name": "Agent A",
    "role": "observer",
    "verified": True,
}


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.005)
    raise AssertionError("The expected asynchronous Stop result did not arrive.")


def body(identifier="stop-1", target="stage"):
    return {"request_id": identifier, "target": target}


def confirmed(_device):
    return {"outcome": "confirmed", "simulated": True}


@pytest.fixture
def opened():
    hosts = []

    def create(dispatch=confirmed, **kwargs):
        host = stops.StopHost(dispatch, **kwargs)
        hosts.append(host)
        return host

    yield create
    for host in hosts:
        host.close()


def completed(host, identifier="stop-1", expected="confirmed"):
    return wait_for(lambda: row if (row := host.get(identifier))["status"] == expected else None)


def deliveries_finished(host, identifier):
    row = host.get(identifier)
    return row if all(item["finished_at"] for item in row["deliveries"]) else None


def test_observer_can_stop_without_operator_or_contract(opened):
    called = []
    host = opened(lambda device: called.append(device) or confirmed(device))
    accepted = host.submit(body(), OBSERVER)
    assert accepted["request_id"] == "stop-1"
    row = completed(host)
    assert called == ["stage"]
    assert row["sender"] == OBSERVER
    assert row["delivery_count"] == 1
    assert row["devices"] == [{"device": "stage", "outcome": "confirmed"}]


def test_repeated_presses_have_separate_requests_without_cooldown(opened):
    called = []
    host = opened(lambda device: called.append(device) or confirmed(device))
    for index in range(3):
        host.submit(body(f"press-{index}"), OBSERVER)
    wait_for(lambda: len(called) == 3)
    assert host.history()["count"] == 3
    assert {row["request_id"] for row in host.history()["items"]} == {
        f"press-{n}" for n in range(3)
    }


def test_transport_copies_group_but_never_suppress_another_stop(opened):
    called = []
    host = opened(lambda device: called.append(device) or confirmed(device))
    first = host.submit(body(), OBSERVER, "http")
    second = host.submit(body(), OBSERVER, "socket")
    assert first["id"] == second["id"]
    row = wait_for(lambda: deliveries_finished(host, first["id"]))
    assert called == ["stage", "stage"]
    assert host.history()["count"] == 1
    assert row["delivery_count"] == 2
    assert row["transports"] == ["http", "socket"]
    assert "deliveries" not in host.history()["items"][0]


def test_unverified_transport_copy_does_not_inherit_verified_sender(opened):
    host = opened()
    host.submit(body(), OBSERVER, "http")
    host.submit(body(), {"name": "Agent A", "kind": "agent", "verified": False}, "fallback")
    row = completed(host)
    assert row["sender"]["kind"] == "mixed"
    assert row["sender"]["verified"] is False
    assert row["deliveries"][0]["sender"] == OBSERVER
    assert row["deliveries"][1]["sender"] == {
        "kind": "unverified",
        "name": "Unverified sender",
        "role": "unknown",
        "verified": False,
    }


def test_later_confirmation_does_not_erase_an_unknown_delivery(opened):
    responses = iter(({"outcome": "unknown"}, {"outcome": "confirmed"}))
    host = opened(lambda _: next(responses))
    host.submit(body(), OBSERVER)
    completed(host, expected="unknown")
    host.submit(body(), OBSERVER, "socket")
    row = wait_for(lambda: deliveries_finished(host, "stop-1"))
    assert row["status"] == "unknown"
    assert [item["status"] for item in row["deliveries"]] == ["unknown", "confirmed"]


def test_slow_target_does_not_delay_other_stop_targets(opened):
    slow = threading.Event()
    called = {name: threading.Event() for name in stops.DEVICES}

    def dispatch(device):
        called[device].set()
        if device == "stage":
            assert slow.wait(3)
        return confirmed(device)

    host = opened(dispatch)
    try:
        host.submit(body(target="all_motion"), OBSERVER)
        assert all(event.wait(1) for event in called.values())
        assert host.get("stop-1")["status"] == "pending"
        slow.set()
        assert len(completed(host)["devices"]) == 3
    finally:
        slow.set()


def test_busy_memory_history_does_not_delay_device_dispatch(opened):
    called = threading.Event()
    host = opened(lambda _: called.set() or {"outcome": "confirmed"})
    with host.lock:
        submitter = threading.Thread(target=host.submit, args=(body(), OBSERVER))
        submitter.start()
        assert called.wait(1)
    submitter.join(2)
    assert not submitter.is_alive()
    completed(host)


def test_slow_store_initialization_does_not_delay_stop(opened, monkeypatch):
    blocked, release, called = threading.Event(), threading.Event(), threading.Event()

    class SlowStore:
        def __init__(self, _path):
            blocked.set()
            assert release.wait(3)

        def load(self):
            return []

        def save(self, _record):
            pass

        def close(self):
            pass

    monkeypatch.setattr(stops, "StopStore", SlowStore)
    host = opened(lambda _: called.set() or {"outcome": "confirmed"})
    try:
        assert blocked.wait(1)
        host.submit(body(), OBSERVER)
        assert called.wait(1)
        assert completed(host)["audit"]["status"] == "starting"
    finally:
        release.set()


def test_blocked_audit_write_does_not_delay_later_stops(opened, monkeypatch):
    blocked, release = threading.Event(), threading.Event()
    called = []

    class SlowStore:
        def __init__(self, _path):
            pass

        def load(self):
            return []

        def save(self, _record):
            blocked.set()
            assert release.wait(3)

        def close(self):
            pass

    monkeypatch.setattr(stops, "StopStore", SlowStore)
    host = opened(lambda device: called.append(device) or confirmed(device))
    try:
        host.submit(body(), OBSERVER)
        assert blocked.wait(1)
        host.submit(body("stop-2"), OBSERVER)
        completed(host, "stop-2")
        assert called == ["stage", "stage"]
        assert host.history()["audit"]["pending"] == 2
    finally:
        release.set()


def test_broken_audit_is_visible_but_stop_still_dispatches(opened, monkeypatch):
    class BrokenStore:
        def __init__(self, _path):
            raise OSError("Audit disk unavailable")

    monkeypatch.setattr(stops, "StopStore", BrokenStore)
    host = opened()
    host.submit(body(), OBSERVER)
    completed(host)
    report = wait_for(lambda: r if (r := host.history())["audit"]["status"] == "failed" else None)
    assert report["audit"]["error"] == "Audit disk unavailable"
    assert report["items"][0]["status"] == "confirmed"
    assert report["items"][0]["audit"]["status"] == "failed"


def test_failed_write_recovers_without_resending_stop(opened, monkeypatch):
    original = StopStore.save
    fail = threading.Event()
    fail.set()
    called = []

    def save(store, record):
        if fail.is_set():
            raise OSError("Audit write failed")
        original(store, record)

    monkeypatch.setattr(StopStore, "save", save)
    host = opened(lambda device: called.append(device) or confirmed(device))
    host.submit(body(), OBSERVER)
    wait_for(lambda: host.history()["audit"]["status"] == "failed")
    fail.clear()
    wait_for(lambda: host.get("stop-1")["audit"]["status"] == "saved")
    assert called == ["stage"]


def test_observe_legacy_stop_never_sends_another_device_call(opened):
    host = opened(lambda _: pytest.fail("An observed Stop must not dispatch."))
    row = host.observe(
        body(target="furnace"),
        {"kind": "human", "name": "Console user", "verified": True},
        "http",
        [{"device": "furnace", "outcome": "pending", "operation_id": "shutdown-1"}],
    )
    assert row["status"] == "pending"
    assert row["deliveries"][0]["outcomes"][0]["operation_id"] == "shutdown-1"
    with pytest.raises(ServiceError, match="supported Stop target"):
        host.submit(body("not-allowed", "apply_setpoint"), OBSERVER)


def test_completed_and_pending_history_survive_restart_without_replay(tmp_path):
    path = tmp_path / "stops.db"
    host = stops.StopHost(confirmed, path)
    host.submit(body(), OBSERVER)
    completed(host)
    host.observe(body("unfinished"), OBSERVER, "http", [{"device": "stage", "outcome": "pending"}])
    wait_for(lambda: host.history()["audit"]["status"] == "saved")
    host.close()
    restored = stops.StopHost(lambda _: pytest.fail("Restart must not replay Stops."), path)
    try:
        wait_for(lambda: restored.history()["count"] == 2)
        assert restored.get("stop-1")["status"] == "confirmed"
        assert restored.get("unfinished")["status"] == "unknown"
        assert restored.get("unfinished")["deliveries"][0]["outcomes"][0]["error"].startswith(
            "The Console restarted"
        )
    finally:
        restored.close()


def test_history_and_delivery_details_are_bounded(opened, monkeypatch):
    monkeypatch.setattr(stops, "HISTORY_LIMIT", 3)
    monkeypatch.setattr(stops, "DELIVERY_LIMIT", 2)
    host = opened()
    for number in range(6):
        host.observe(
            body(f"stop-{number}"), OBSERVER, "http", [{"device": "stage", "outcome": "confirmed"}]
        )
    assert host.history()["count"] == 3
    assert host.history()["items"][0]["request_id"] == "stop-5"
    for number in range(4):
        host.observe(
            body("copies"),
            OBSERVER,
            f"transport-{number}",
            [{"device": "stage", "outcome": "confirmed"}],
        )
    row = host.get("copies")
    assert row["delivery_count"] == 4
    assert len(row["deliveries"]) == 2
    assert row["deliveries_truncated"] is True
    assert row["status"] == "confirmed"


def test_truncation_preserves_earlier_unknown_outcome(opened, monkeypatch):
    monkeypatch.setattr(stops, "DELIVERY_LIMIT", 1)
    host = opened()
    for outcome in ("unknown", "confirmed", "confirmed"):
        host.observe(body(), OBSERVER, "http", [{"device": "stage", "outcome": outcome}])
    assert host.get("stop-1")["status"] == "unknown"


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"request_id": "x", "target": "../stage"},
        {"request_id": "x", "target": "stage", "sender": "owner"},
        {"request_id": "", "target": "stage"},
        {"request_id": "x", "target": ["stage"]},
    ],
)
def test_invalid_body_cannot_choose_an_arbitrary_dispatch(opened, value):
    host = opened(lambda _: pytest.fail("An invalid request must not dispatch."))
    with pytest.raises(ServiceError):
        host.submit(value, OBSERVER)


def test_same_request_id_for_different_targets_is_not_suppressed(opened):
    host = opened()
    stage = host.submit(body(), OBSERVER)
    robot = host.submit(body(target="robot"), OBSERVER)
    assert stage["id"] != robot["id"]
    completed(host, stage["id"])
    completed(host, robot["id"])
    with pytest.raises(ServiceError, match="record ID"):
        host.get("stop-1")


@pytest.mark.parametrize(
    "value", [None, {"outcome": "ok"}, {"outcome": "confirmed", "secret": "not-public"}]
)
def test_dispatch_result_is_reduced_to_documented_fields(opened, value):
    host = opened(lambda _: value)
    host.submit(body(), OBSERVER)
    row = completed(
        host, expected="confirmed" if value and value["outcome"] == "confirmed" else "unknown"
    )
    assert "secret" not in row["deliveries"][0]["outcomes"][0]


def test_dispatch_exception_and_disabled_policy_are_not_confirmed(opened):
    def unavailable(_):
        raise OSError("No device")

    failing = opened(unavailable)
    failing.submit(body(), OBSERVER)
    assert completed(failing, expected="unknown")["devices"][0]["outcome"] == "unknown"
    disabled = opened(lambda _: {"outcome": "disabled"})
    disabled.submit(body(), OBSERVER)
    assert completed(disabled, expected="disabled")["status"] == "disabled"


@pytest.mark.parametrize("error", [None, "Specific failure"])
def test_dispatch_reason_is_bounded_and_preserved_when_error_is_absent(opened, error):
    host = opened(lambda _: {"outcome": "disabled", "error": error, "reason": "r" * 300})
    host.submit(body(), OBSERVER)
    row = completed(host, expected="disabled")
    assert row["deliveries"][0]["outcomes"] == [
        {"device": "stage", "outcome": "disabled", "error": error or "r" * 256}
    ]


def test_observed_malformed_outcome_stays_unknown(opened):
    host = opened()
    row = host.observe(body(), OBSERVER, outcomes=[{"device": "stage", "outcome": []}])
    assert row["status"] == "unknown"


def test_slow_publisher_does_not_delay_stop_dispatch(opened):
    blocked, release = threading.Event(), threading.Event()
    called = []

    def publish(_kind, _summary):
        blocked.set()
        assert release.wait(3)

    host = opened(lambda device: called.append(device) or confirmed(device), publish=publish)
    try:
        host.submit(body(), OBSERVER)
        assert blocked.wait(1)
        host.submit(body("stop-2"), OBSERVER)
        completed(host, "stop-2")
        assert called == ["stage", "stage"]
    finally:
        release.set()


def test_incompatible_store_is_reported_without_overwriting_it(tmp_path, opened):
    path = tmp_path / "wrong.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.execute("INSERT INTO unrelated VALUES ('keep')")
    host = opened(path=path)
    host.submit(body(), OBSERVER)
    completed(host)
    wait_for(lambda: host.history()["audit"]["status"] == "failed")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM unrelated").fetchone() == ("keep",)


def test_store_retains_a_fixed_number_of_records(tmp_path, monkeypatch):
    from orchestrator.agent_connect import stop_store

    monkeypatch.setattr(stop_store, "HISTORY_LIMIT", 2)
    store = StopStore(tmp_path / "stops.db")
    try:
        for number in range(4):
            store.save({"id": str(number), "revision": 1, "updated_at": number, "deliveries": []})
        assert [record["id"] for record in store.load()] == ["3", "2"]
    finally:
        store.close()


def test_sqlite_lock_cannot_delay_a_device_stop(tmp_path, opened):
    path = tmp_path / "stops.db"
    called = threading.Event()
    host = opened(lambda _: called.set() or {"outcome": "confirmed"}, path=path)
    wait_for(lambda: host.ready)
    with sqlite3.connect(path) as connection:
        connection.execute("BEGIN EXCLUSIVE")
        host.submit(body(), OBSERVER)
        assert called.wait(1)
        completed(host)
        wait_for(lambda: host.history()["audit"]["status"] == "failed")
    wait_for(lambda: host.get("stop-1")["audit"]["status"] == "saved")


def test_one_saved_record_does_not_hide_another_record_audit_failure(opened, monkeypatch):
    original = StopStore.save

    def save(store, record):
        if record["request_id"] == "bad":
            raise OSError("This audit record failed")
        original(store, record)

    monkeypatch.setattr(StopStore, "save", save)
    host = opened()
    host.submit(body("bad"), OBSERVER)
    host.submit(body("good"), OBSERVER)
    wait_for(lambda: host.get("good")["audit"]["status"] == "saved")
    wait_for(lambda: host.get("bad")["audit"]["status"] == "failed")
    assert host.history()["audit"]["status"] == "failed"
    assert host.get("bad")["status"] == "confirmed"


def test_copy_during_slow_startup_merges_without_another_dispatch(tmp_path, monkeypatch):
    path = tmp_path / "stops.db"
    initial = stops.StopHost(confirmed, path)
    old_id = initial.submit(body(), OBSERVER)["id"]
    completed(initial)
    wait_for(lambda: initial.get(old_id)["audit"]["status"] == "saved")
    initial.close()
    entered, release = threading.Event(), threading.Event()
    original = StopStore.load

    def load(store):
        entered.set()
        assert release.wait(3)
        return original(store)

    monkeypatch.setattr(StopStore, "load", load)
    called = []
    host = stops.StopHost(lambda device: called.append(device) or confirmed(device), path)
    try:
        assert entered.wait(1)
        current = host.submit(body(), OBSERVER, "socket")
        completed(host)
        release.set()
        wait_for(lambda: host.ready and host.history()["audit"]["status"] == "saved")
        assert host.history()["count"] == 1
        row = host.get(old_id)
        assert row["id"] == current["id"]
        assert row["delivery_count"] == 2
        assert row["transports"] == ["http", "socket"]
        assert called == ["stage"]
    finally:
        release.set()
        host.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM stops").fetchone()[0] == 1
