import threading

import pytest

from labkit.errors import ServiceError
from labkit.lease import Lease


class Clock:
    now = 10

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


def test_lease_deadline_renewal_and_once_only_expiry(clock):
    expired = []
    lease = Lease(3, clock, on_expire=lambda: expired.append(clock()))
    assert lease.holder is None
    assert not lease.expired
    assert lease.acquire("a") == {"holder": "a", "remaining_s": 3}
    clock.now = 12.9
    assert lease.renew("a")["remaining_s"] == 3
    clock.now = 15.8
    assert not lease.poll()
    clock.now = 15.9
    assert lease.expired
    assert lease.poll()
    assert lease.holder is None
    assert lease.expired
    assert not lease.poll()
    assert expired == [15.9]
    lease.acquire("b")
    assert not lease.expired
    clock.now += 3
    lease.poll()
    assert expired == [15.9, 18.9]


def test_conflicts_release_and_renew_require_current_holder(clock):
    lease = Lease(3, clock)
    with pytest.raises(ServiceError, match="Acquire") as error:
        lease.renew("a")
    assert error.value.status == 409
    lease.acquire("a")
    for operation in (lease.acquire, lease.renew, lease.release):
        with pytest.raises(ServiceError) as error:
            operation("b")
        assert error.value.status == 409
        assert lease.holder == "a"
    assert lease.release("a")["holder"] is None
    assert not lease.expired
    assert lease.release("a")["holder"] is None


def test_expired_holder_cannot_renew_but_explicit_heartbeat_can_acquire(clock):
    calls = []
    lease = Lease(3, clock, on_expire=lambda: calls.append("stop"))
    lease.acquire("a")
    clock.now += 3
    with pytest.raises(ServiceError, match="Acquire"):
        lease.renew("a")
    assert calls == ["stop"]
    lease.acquire("a")
    assert lease.holder == "a"


def test_acquisition_waits_for_callback_completion_without_holding_lease_lock(clock):
    entered, finish = threading.Event(), threading.Event()
    calls = []

    def expire():
        calls.append(1)
        entered.set()
        assert finish.wait(2)

    lease = Lease(3, clock, on_expire=expire)
    lease.acquire("a")
    clock.now += 3
    thread = threading.Thread(target=lease.poll)
    thread.start()
    try:
        assert entered.wait(2)
        assert lease.holder is None
        assert lease.snapshot()["holder"] is None
        assert not lease.poll()
        for operation in (lease.acquire, lease.renew, lease.release):
            with pytest.raises(ServiceError, match="in progress"):
                operation("b")
        assert calls == [1]
    finally:
        finish.set()
        thread.join(2)
    assert not thread.is_alive()
    lease.acquire("b")
    assert lease.holder == "b"


def test_racing_pollers_call_expiry_once(clock):
    calls = []
    lease = Lease(3, clock, on_expire=lambda: calls.append(1))
    lease.acquire("a")
    clock.now += 3
    threads = [threading.Thread(target=lease.poll) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert calls == [1]


def test_failed_expiry_is_not_retried_and_blocks_new_control(clock):
    calls = []

    def expire():
        calls.append(1)
        raise RuntimeError("stop outcome unknown")

    lease = Lease(3, clock, on_expire=expire)
    lease.acquire("a")
    clock.now += 3
    with pytest.raises(RuntimeError, match="unknown"):
        lease.poll()
    assert not lease.poll()
    assert lease.holder is None
    with pytest.raises(ServiceError, match="expiry failed"):
        lease.acquire("b")
    assert calls == [1]


def test_expiry_cannot_overtake_authorized_command_dispatch(clock):
    entered, finish, expired, polling = (threading.Event() for _ in range(4))
    lease = Lease(3, clock, on_expire=expired.set)
    lease.acquire("a")

    def dispatch():
        with lease.control("a"):
            entered.set()
            assert finish.wait(2)

    def poll():
        polling.set()
        lease.poll()

    command = threading.Thread(target=dispatch)
    command.start()
    assert entered.wait(2)
    clock.now += 3
    watcher = threading.Thread(target=poll)
    watcher.start()
    try:
        assert polling.wait(2)
        assert not expired.is_set()
    finally:
        finish.set()
        command.join(2)
        watcher.join(2)
    assert not command.is_alive() and not watcher.is_alive()
    assert expired.is_set()


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan"), True, "3"])
def test_invalid_durations(seconds):
    with pytest.raises(ValueError):
        Lease(seconds)


@pytest.mark.parametrize("client", [None, "", "a" * 129, "a\n", "é", "\x7f", 1])
def test_invalid_client_ids(client, clock):
    lease = Lease(3, clock)
    for operation in (lease.acquire, lease.renew, lease.release):
        with pytest.raises(ValueError):
            operation(client)


@pytest.mark.parametrize("client", [" ", "a", "~" * 128])
def test_client_id_boundaries(client, clock):
    lease = Lease(3, clock)
    lease.acquire(client)
    assert lease.holder == client


def test_release_rechecks_deadline_after_poll_before_clearing_holder(clock, monkeypatch):
    calls = []
    lease = Lease(3, clock, on_expire=lambda: calls.append("expired"))
    lease.acquire("a")
    poll = lease.poll
    advanced = False

    def racing_poll():
        nonlocal advanced
        result = poll()
        if not advanced:
            advanced = True
            clock.now += 3
        return result

    monkeypatch.setattr(lease, "poll", racing_poll)
    assert lease.release("a")["holder"] is None
    assert calls == ["expired"]
