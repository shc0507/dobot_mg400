"""The PC relay binds commands to its stored session and selected equipment source."""

import threading

import pytest
from flask import Flask

from labkit.errors import ServiceError
from orchestrator.agent_connect.control_portal import (
    ControlTransport,
    PortalControl,
    register_control_portal,
)

BASE = "/api/agent-control/"


@pytest.fixture
def relay():
    calls = []
    state = {
        "phase": 5,
        "commands_enabled": True,
        "physical_commands_enabled": False,
        "furnace_control_enabled": False,
        "equipment_mode": "simulated",
        "principal": {"id": "session-a", "kind": "agent"},
        "operator": {"id": "session-a", "kind": "agent"},
        "boot_id": "boot-a",
        "control_epoch": 7,
    }

    def transport(source, action, data=None, *, token=None):
        calls.append((source, action, data, token))
        if action == "status":
            return dict(state)
        if action == "commands/catalog":
            return {"version": 1, "contract_id": "sha256:catalog", "actions": []}
        if action == "commands/history":
            return {"items": [{"request_id": "request-1", "status": "running"}], "count": 1}
        return {"request_id": "request-1", "status": "running"}

    app = Flask(__name__)
    control = register_control_portal(app, transport)
    control.records["mock"]["session_token"] = "stored-token"
    control.records["live"]["session_token"] = "stored-live-token"
    return app.test_client(), control, state, calls


def envelope(**changes):
    return {
        "source": "mock",
        "request_id": "request-1",
        "boot_id": "boot-a",
        "contract_id": "sha256:catalog",
        "control_epoch": 7,
        "action": "stage.move",
        "params": {"x": 10},
        "preconditions": {},
        **changes,
    }


def test_command_uses_memory_session_and_returns_operation(relay):
    client, _control, _state, calls = relay
    response = client.post(
        BASE + "commands", json=envelope(), headers={"Authorization": "Bearer caller-token"}
    )
    assert response.status_code == 200
    assert response.json == {"request_id": "request-1", "status": "running"}
    assert calls[-1] == (
        "mock",
        "commands",
        {key: value for key, value in envelope().items() if key != "source"},
        "stored-token",
    )
    assert "stored-token" not in response.text


@pytest.mark.parametrize("field", ["session_token", "credential", "token", "url"])
def test_command_rejects_caller_credentials_and_extra_fields(relay, field):
    client, _control, _state, calls = relay
    assert client.post(BASE + "commands", json=envelope(**{field: "caller"})).status_code == 400
    assert calls == []


@pytest.mark.parametrize(
    "request_id", ["../status", "id?token=x", "id/other", "id%2fother", "", "x" * 129]
)
def test_command_rejects_invalid_request_ids(relay, request_id):
    client, _control, _state, calls = relay
    assert client.post(BASE + "commands", json=envelope(request_id=request_id)).status_code == 400
    assert calls == []


def live_state(state):
    state.update(
        phase=8,
        commands_enabled=True,
        physical_commands_enabled=True,
        furnace_control_enabled=True,
        equipment_mode="physical",
        command_contract_id="sha256:catalog",
    )


def test_live_dispatch_requires_current_physical_contract_and_operator(relay):
    client, _control, state, calls = relay
    assert client.post(BASE + "commands", json=envelope(source="live")).status_code == 409
    live_state(state)
    response = client.post(BASE + "commands", json=envelope(source="live"))
    assert response.status_code == 200
    assert calls[-1][0:2] == ("live", "commands")
    assert calls[-1][3] == "stored-live-token"
    state["operator"] = None
    assert client.post(BASE + "commands", json=envelope(source="live")).status_code == 403
    assert (
        client.post(
            BASE + "commands/cancel", json={"source": "live", "request_id": "request-1"}
        ).status_code
        == 200
    )


def test_phase_eight_disabled_live_server_allows_connection_status_only(relay):
    client, _control, state, calls = relay
    state.update(
        phase=8,
        commands_enabled=False,
        physical_commands_enabled=False,
        furnace_control_enabled=False,
        equipment_mode="disabled",
        command_contract_id="sha256:catalog",
    )
    response = client.get(BASE + "status?source=live")
    assert response.status_code == 200
    assert response.json["connected"] is True
    assert response.json["commands_enabled"] is False
    assert client.post(BASE + "commands", json=envelope(source="live")).status_code == 403
    assert all(call[1] != "commands" for call in calls)


@pytest.mark.parametrize(
    "field,value", [("boot_id", "old"), ("control_epoch", 6), ("contract_id", "old")]
)
def test_live_dispatch_rejects_stale_envelope_before_command(relay, field, value):
    client, _control, state, calls = relay
    live_state(state)
    assert (
        client.post(BASE + "commands", json=envelope(source="live", **{field: value})).status_code
        == 409
    )
    assert all(call[1] != "commands" for call in calls)


def test_observer_cannot_send_but_can_cancel_own_request(relay):
    client, _control, state, calls = relay
    state["operator"] = {"id": "human"}
    assert client.post(BASE + "commands", json=envelope()).status_code == 403
    assert all(call[1] != "commands" for call in calls)
    response = client.post(
        BASE + "commands/cancel", json={"source": "mock", "request_id": "request-1"}
    )
    assert response.status_code == 200
    assert calls[-1] == ("mock", "commands/cancel", {"request_id": "request-1"}, "stored-token")


def test_expired_session_cannot_cancel_after_status_removes_held_token(relay):
    client, control, state, calls = relay
    state["principal"] = None
    response = client.post(
        BASE + "commands/cancel", json={"source": "mock", "request_id": "request-1"}
    )
    assert response.status_code == 401
    assert "session_token" not in control.records["mock"]
    assert all(call[1] != "commands/cancel" for call in calls)


@pytest.mark.parametrize(
    "field,value",
    [
        ("furnace_control_enabled", True),
        ("physical_commands_enabled", True),
        ("equipment_mode", "physical"),
    ],
)
def test_unsafe_upstream_status_cannot_enable_relay(relay, field, value):
    client, _control, state, calls = relay
    state[field] = value
    assert client.post(BASE + "commands", json=envelope()).status_code == 409
    assert all(call[1] != "commands" for call in calls)


def test_history_and_lookup_use_stored_token(relay):
    client, _control, _state, calls = relay
    assert client.get(BASE + "commands/history?source=mock").json["count"] == 1
    assert (
        client.get(BASE + "commands/request/request-1?source=mock").json["request_id"]
        == "request-1"
    )
    assert all(call[3] == "stored-token" for call in calls)
    assert client.get(BASE + "commands/history?source=mock&token=caller").status_code == 400


def test_command_and_lookup_accept_engine_request_ids_with_colons(relay):
    client, _control, _state, calls = relay
    assert (
        client.post(BASE + "commands", json=envelope(request_id="client:request-1")).status_code
        == 200
    )
    assert client.get(BASE + "commands/request/client:request-1?source=mock").status_code == 200
    assert calls[-1][1] == "commands/request/client:request-1"


def test_public_catalog_and_authenticated_history(relay):
    client, control, _state, calls = relay
    control.records["mock"].clear()
    assert client.get(BASE + "commands/catalog?source=mock").status_code == 200
    assert calls[-1][3] is None
    assert client.get(BASE + "commands/history?source=mock").status_code == 401
    assert client.post(BASE + "commands", json=envelope()).status_code == 401


@pytest.mark.parametrize(
    "action",
    [
        "commands/request/../status",
        "commands/request/id?x=y",
        "commands/request/id%2fother",
        "commands/request/id/other",
        "commands/admin",
    ],
)
def test_transport_rejects_paths_before_opening_network(action):
    with pytest.raises(ValueError):
        ControlTransport()("mock", action)


def test_relay_rejects_unsupported_read_and_write_routes(relay):
    client, _control, _state, calls = relay
    assert client.get(BASE + "commands/admin?source=mock").status_code == 404
    assert client.post(BASE + "commands/history", json={"source": "mock"}).status_code == 404
    assert client.get(BASE + "commands/cancel?source=mock").status_code == 404
    assert calls == []


def test_source_and_foreign_origin_are_checked_before_command(relay):
    client, _control, _state, calls = relay
    assert client.post(BASE + "commands", json=envelope(source=[])).status_code == 400
    assert (
        client.post(
            BASE + "commands", json=envelope(), headers={"Origin": "http://other.example"}
        ).status_code
        == 403
    )
    assert calls == []


def test_phase_six_choices_use_held_session(relay):
    client, _control, state, calls = relay
    state["phase"] = 6
    assert client.get(BASE + "status?source=mock").status_code == 200
    assert client.get(BASE + "commands/choices?source=mock").status_code == 200
    assert calls[-1] == ("mock", "commands/choices", None, "stored-token")
    assert client.post(BASE + "commands", json=envelope()).status_code == 200


def test_stop_without_connection_ignores_caller_credentials_and_status(relay):
    client, control, _state, calls = relay
    control.records["mock"].clear()
    response = client.post(
        BASE + "stop",
        json={"source": "mock", "request_id": "stop:1", "target": "all_motion"},
        headers={"Authorization": "Bearer caller-token"},
    )
    assert response.status_code == 200
    assert calls == [("mock", "stop", {"request_id": "stop:1", "target": "all_motion"}, None)]


def test_stop_uses_known_token_but_does_not_refresh_its_validity(relay):
    client, _control, _state, calls = relay
    response = client.post(
        BASE + "stop", json={"source": "mock", "request_id": "stop:1", "target": "stage"}
    )
    assert response.status_code == 200
    assert calls == [("mock", "stop", {"request_id": "stop:1", "target": "stage"}, "stored-token")]


@pytest.mark.parametrize(
    "changes",
    [
        {"target": "unknown-device"},
        {"target": []},
        {"request_id": "../status"},
        {"request_id": "id/other"},
        {"request_id": None},
        {"session_token": "caller"},
        {"sender": "claimed owner"},
        {"url": "http://elsewhere"},
    ],
)
def test_stop_rejects_invalid_target_path_or_claimed_identity(relay, changes):
    client, _control, _state, calls = relay
    response = client.post(
        BASE + "stop",
        json={"source": "mock", "request_id": "stop:1", "target": "stage", **changes},
    )
    assert response.status_code == 400
    assert calls == []


@pytest.mark.parametrize("target", ["stage", "furnace", "gas_all", "gas_0", "gas_1", "gas_2"])
def test_live_stop_ignores_status_and_requires_no_session(relay, target):
    client, control, _state, calls = relay
    control.records["live"].clear()
    body = {"source": "live", "request_id": "stop:1", "target": target}
    assert client.post(BASE + "stop", json=body).status_code == 200
    assert calls == [("live", "stop", {"request_id": "stop:1", "target": target}, None)]


def test_stop_does_not_wait_for_normal_relay_lock():
    delivered = threading.Event()
    release = threading.Event()
    held = threading.Event()

    def transport(_source, _action, _data=None, **_kwargs):
        delivered.set()
        return {"status": "confirmed"}

    control = PortalControl(transport)

    def block_normal_requests():
        with control.lock:
            held.set()
            release.wait(2)

    thread = threading.Thread(target=block_normal_requests)
    thread.start()
    assert held.wait(1)
    result = []
    stopper = threading.Thread(
        target=lambda: result.append(
            control.action("mock", "stop", {"request_id": "stop:1", "target": "stage"})
        )
    )
    try:
        stopper.start()
        assert delivered.wait(0.5), "Stop waited for the ordinary command lock"
    finally:
        release.set()
        thread.join(2)
        stopper.join(2)
    assert result == [{"status": "confirmed"}]


def test_stop_history_and_details_need_no_session(relay):
    client, control, _state, calls = relay
    control.records["mock"].clear()
    assert client.get(BASE + "stops?source=mock").status_code == 200
    assert client.get(BASE + "stops/stop:1?source=mock").status_code == 200
    assert calls == [("mock", "stops", None, None), ("mock", "stops/stop:1", None, None)]
    assert client.get(BASE + "stops?source=mock&token=caller").status_code == 400
    assert client.get(BASE + "stops/a/other?source=mock").status_code in {404, 405}


def test_stop_fallback_uses_same_request_and_no_credentials():
    calls = []

    class Transport:
        def __call__(self, source, action, data=None, *, token=None):
            calls.append(("primary", source, action, data, token))
            raise ServiceError(503, "Console unavailable")

        def fallback(self, source, action, data=None):
            calls.append(("fallback", action, data))
            return {"status": "confirmed", "relay_source": "fallback"}

    control = PortalControl(Transport())
    control.records["mock"]["session_token"] = "expired-token"
    body = {"request_id": "stop:1", "target": "stage"}
    assert control.action("mock", "stop", body)["relay_source"] == "fallback"
    assert calls == [
        ("primary", "mock", "stop", body, "expired-token"),
        ("fallback", "stop", body),
    ]
    history = control.read_stops("mock", "stops")
    assert history["source_status"]["fallback"]["available"] is True
    assert history["source_status"]["console"]["available"] is False


@pytest.mark.parametrize("status", [400, 401, 403, 409])
def test_stop_does_not_use_fallback_to_bypass_server_rejection(status):
    class Transport:
        def __call__(self, *_args, **_kwargs):
            raise ServiceError(status, "Rejected")

        def fallback(self, *_args):
            pytest.fail("A server rejection must not trigger fallback")

    with pytest.raises(ServiceError) as caught:
        PortalControl(Transport()).stop("mock", {"request_id": "stop:1", "target": "stage"})
    assert caught.value.status == status


def test_fallback_has_fixed_stop_only_paths(monkeypatch):
    transport = ControlTransport()
    calls = []
    monkeypatch.setattr(
        transport, "_request", lambda *args: calls.append(args) or {"status": "confirmed"}
    )
    body = {"request_id": "stop:1", "target": "stage"}
    assert transport.fallback("mock", "stop", body)["relay_source"] == "fallback"
    assert calls == [("http://127.0.0.1:8794/stop", body, None, "mock")]
    for path in ["commands", "release", "http://elsewhere", "stops/../status", "stops/id/other"]:
        with pytest.raises(ValueError):
            transport.fallback("mock", path)


def stop_record(identifier, *, verified=True, status="confirmed"):
    return {
        "request_id": identifier,
        "target": "stage",
        "requested_at": 10,
        "updated_at": 11,
        "sender": {"name": "Agent" if verified else "Fallback caller", "verified": verified},
        "status": status,
        "deliveries": [{"transport": "http" if verified else "fallback"}],
    }


def test_history_keeps_fallback_records_after_console_recovers():
    primary = stop_record("shared")
    fallback = stop_record("shared", verified=False)
    fallback_only = stop_record("fallback-only", verified=False)
    calls = []

    class Transport:
        def __call__(self, source, action, data=None, *, token=None):
            calls.append((source, action, token))
            return {"items": [primary], "audit": {"status": "saved"}}

        def fallback(self, source, action, data=None):
            calls.append(("fallback", action))
            return {"items": [fallback, fallback_only], "audit": {"status": "failed"}}

    result = PortalControl(Transport()).read_stops("mock", "stops")
    assert result["count"] == 2
    shared = next(item for item in result["items"] if item["request_id"] == "shared")
    assert shared["relay_sources"] == {"console": primary, "fallback": fallback}
    assert result["source_status"]["fallback"]["audit"]["status"] == "failed"
    assert calls == [("mock", "stops", None), ("fallback", "stops")]


def test_detail_uses_fallback_when_console_has_no_matching_stop():
    expected = stop_record("fallback-only", verified=False)

    class Transport:
        def __call__(self, *_args, **_kwargs):
            raise ServiceError(404, "Unknown Stop")

        def fallback(self, source, action, data=None):
            assert action == "stops/fallback-only"
            return expected

    result = PortalControl(Transport()).read_stops("mock", "stops/fallback-only")
    assert result["relay_sources"] == {"fallback": expected}


def test_detail_retains_both_transport_delivery_lists():
    primary = stop_record("shared")
    primary["updated_at"] = 20
    fallback = stop_record("shared", verified=False)

    class Transport:
        def __call__(self, *_args, **_kwargs):
            return primary

        def fallback(self, *_args):
            return fallback

    result = PortalControl(Transport()).read_stops("mock", "stops/shared")
    assert result["relay_sources"] == {"console": primary, "fallback": fallback}
    assert result["updated_at"] == 20


def test_live_history_reads_only_live_emergency_fallback():
    class Transport:
        def __call__(self, source, action, data=None, *, token=None):
            assert source == "live"
            return {"items": []}

        def fallback(self, source, action, data=None):
            assert source == "live"
            return {"items": [stop_record("live-emergency")]}

    result = PortalControl(Transport()).read_stops("live", "stops")
    assert result["count"] == 1
    assert result["items"][0]["request_id"] == "live-emergency"


def test_live_fallback_uses_fixed_emergency_routes(monkeypatch):
    transport = ControlTransport()
    calls = []
    monkeypatch.setattr(transport, "_request", lambda *args: calls.append(args) or {})
    for action in ("stop", "status", "stops", "stops/request:1"):
        transport.fallback("live", action)
        assert calls[-1] == (
            "http://100.64.0.1:8082/lab/api/agent/emergency/" + action,
            None,
            None,
            "live",
        )
    with pytest.raises(ValueError):
        transport.fallback("http://elsewhere", "stop")
