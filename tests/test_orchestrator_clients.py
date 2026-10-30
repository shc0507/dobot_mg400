"""Lease adapters use strict renewal and isolate cleanup failures."""

import threading
import time

import pytest

from labkit.errors import ServiceError
from labkit.lease import Lease
from orchestrator.clients import Clients, ServiceAdapter, from_settings


def lease_adapter(monkeypatch):
    now = [10.0]
    lease = Lease(clock=lambda: now[0])
    paths = []
    adapter = ServiceAdapter("http://127.0.0.1:1", "", "orch-unique-run")

    def request(path, data=None):
        paths.append(path)
        if path == "api/describe":
            return {"contract": 1, "lease_seconds": 3}
        if path == "api/heartbeat":
            return {"lease": lease.acquire(adapter.identity)}
        if path == "api/renew":
            return {"lease": lease.renew(adapter.identity)}
        if path == "api/state":
            return {
                "lease": lease.snapshot(),
                "busy": False,
                "locked": False,
                "fault": None,
                "connected": True,
            }
        if path == "api/stop":
            return {"stop": "confirmed"}
        if path == "api/release":
            return {"lease": lease.release(adapter.identity)}
        pytest.fail(f"Unexpected offline request: {path}")

    monkeypatch.setattr(adapter._http, "_request", request)
    return adapter, lease, now, paths


@pytest.mark.parametrize("loss", ["expired", "released", "other-holder"])
def test_atomic_renewal_never_reacquires_a_lost_lease(monkeypatch, loss):
    adapter, lease, now, paths = lease_adapter(monkeypatch)
    adapter.describe()
    adapter.acquire()
    now[0] += 1
    adapter.heartbeat()
    assert paths.count("api/heartbeat") == 1 and paths.count("api/renew") == 1
    if loss == "expired":
        now[0] += 4
    else:
        lease.release(adapter.identity)
        if loss == "other-holder":
            lease.acquire("other")
    with pytest.raises(ServiceError, match="Acquire the control lease"):
        adapter.heartbeat()
    assert paths.count("api/heartbeat") == 1
    assert lease.holder == ("other" if loss == "other-holder" else None)


def test_missing_renew_route_fails_closed_without_heartbeat_fallback(monkeypatch):
    adapter, lease, now, paths = lease_adapter(monkeypatch)
    adapter.describe()
    adapter.acquire()
    original = adapter._http._request

    def legacy(path, data=None):
        if path == "api/renew":
            raise ServiceError(404, "missing renew route")
        return original(path, data)

    monkeypatch.setattr(adapter._http, "_request", legacy)
    with pytest.raises(ServiceError) as error:
        adapter.heartbeat()
    assert error.value.status == 404 and paths.count("api/heartbeat") == 1


def test_renew_loss_is_terminal_even_if_another_process_restores_ownership(monkeypatch):
    adapter, lease, now, paths = lease_adapter(monkeypatch)
    pool = Clients({"stage": adapter})
    pool.acquire(("stage",), cancelled=threading.Event())
    try:
        now[0] += 4
        deadline = time.monotonic() + 2
        while pool.error is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pool.error is not None
        lease.acquire(adapter.identity)
        pool.stop_all()
        with pytest.raises(RuntimeError, match="Lease failure cannot be resumed"):
            pool.reconcile()
        assert paths.count("api/heartbeat") == 1
        assert paths.count("api/renew") == 1
    finally:
        while pool.release() is False:
            time.sleep(0.01)


def test_release_and_close_errors_do_not_skip_other_services():
    calls = []

    class Service:
        def __init__(self, name):
            self.identity = "one-shared-run-identity"
            self.name = name

        def release(self):
            calls.append((self.name, "release"))
            raise RuntimeError("release failed")

        def close(self):
            calls.append((self.name, "close"))
            raise RuntimeError("close failed")

    pool = Clients({name: Service(name) for name in ("a", "b")})
    pool.acquired = ["a", "b"]
    errors = pool.release()
    assert calls == [("a", "release"), ("b", "release"), ("a", "close"), ("b", "close")]
    assert set(errors) == {"a", "b", "close:a", "close:b"}


def test_adapter_construction_makes_no_requests_and_disables_ambient_proxies(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://production.invalid:9")
    clients = from_settings(
        {"MG400_ORCH_STAGE_URL": "http://127.0.0.1:1/contract"}, identity="orch-config-only"
    )
    assert set(clients) == {"stage"}
    handlers = clients["stage"]._http._opener.handlers
    assert not any(getattr(handler, "proxies", {}) for handler in handlers)
    redirect = next(handler for handler in handlers if hasattr(handler, "redirect_request"))
    assert redirect.redirect_request(None, None, 302, "", {}, "http://other.invalid") is None
