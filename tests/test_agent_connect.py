"""Phase 0 protocol checks without a device or external network."""

import copy
import json
import socket
from types import SimpleNamespace

import pytest
from flask import Blueprint, Flask

from orchestrator.agent_connect.client import DISCOVERY, ORIGIN, LocalTransport, ScriptedClient
from orchestrator.agent_connect.contract import canonical_bytes, digest, fixture
from orchestrator.agent_connect.mock import MockConsole, create_mock_app
from orchestrator.agent_connect.portal import create_portal_app
from orchestrator.agent_connect.scenarios import run_scenarios
from orchestrator.web.workspaces.agent_connect import register


@pytest.fixture
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("The Phase 0 test attempted an external network connection.")

    monkeypatch.setattr(socket.socket, "connect", forbidden)


def in_process(http):
    def transport(path, data=None):
        response = http.get(path) if data is None else http.post(path, json=data)
        assert response.status_code == 200
        return response.get_json()

    return transport


def test_scenarios_pass_without_network_and_export_exact_evidence(offline):
    result = run_scenarios()
    assert result["passed"], result["results"]
    assert all(row["actual"] == row["expected"] for row in result["results"])
    assert len(set(result["contract_ids"].values())) == 3
    assert result["fixture_versions"] == {"A": 1, "B": 2}
    assert result["messages"]["A"][-1]["response"]["stage"]["position"] == 0


def test_mock_pause_preserves_sample_time_and_contract_identity(offline):
    ticks = [0.0]
    source = MockConsole(clock=lambda: ticks[0], wall=lambda: ticks[0] + 100)
    initial = source.snapshot()
    ticks[0] = 1
    moving = source.snapshot()
    assert moving["stage"]["position"] != initial["stage"]["position"]
    assert moving["stage"]["reference_revision"] == initial["stage"]["reference_revision"]
    source.control("pause")
    ticks[0] = 5
    stale = source.snapshot()
    assert stale["quality"] == "stale"
    assert stale["sampled_at"] == moving["sampled_at"]
    assert stale["seq"] == moving["seq"]
    assert stale["contract_id"] == initial["contract_id"]
    source.control("reference-reset")
    assert source.snapshot()["stage"]["reference_revision"] == 2
    assert source.snapshot()["stage"]["position"] == moving["stage"]["position"]
    source.control("restart")
    assert source.snapshot()["boot_id"] != stale["boot_id"]
    assert source.snapshot()["samples"] == initial["samples"]


def test_contract_digest_is_canonical_and_rejects_unsupported_numbers():
    assert digest({"b": 2, "a": 1}) == digest({"a": 1, "b": 2})
    assert canonical_bytes({"a": "°C"}) == b'{"a":"\xc2\xb0C"}'
    for invalid in ({"a": 1.5}, {"a": float("nan")}, {"a": 2**53}, {"非ASCII": 1}):
        with pytest.raises(ValueError):
            canonical_bytes(invalid)


def test_clients_preserve_separate_caches_and_detect_changes_without_events(offline):
    source = MockConsole()
    transport = in_process(create_mock_app(source).test_client())
    a, b = ScriptedClient("A", 1, transport), ScriptedClient("B", 2, transport)
    assert a.poll()["compatibility"]["status"] == "compatible"
    assert b.poll()["compatibility"]["status"] == "supported subset"
    baseline = a.cached_id
    source.control("fixture", "compatible")
    assert a.poll()["compatibility"]["status"] == "compatible"
    assert a.cached_id != baseline
    assert b.cached_id == baseline
    assert any(row["path"] == "schemas.state.properties.source_label" for row in a.changes)
    source.control("fixture", "incompatible")
    report = a.poll()
    assert report["compatibility"]["status"] == "client update required"
    assert "rotation" in " ".join(report["compatibility"]["problems"])
    assert report["state"] is None
    assert report["raw_state"]["stage"]["unit"] == "mm"
    assert not report["normal_commands"]


@pytest.mark.parametrize(
    "changed",
    [
        {"mode": "LIVE"},
        {"contract_url": "http://100.64.0.1:8080/lab/api/agent/contracts/unsafe.json"},
        {"endpoints": {"state": "http://hardware.invalid/state"}},
    ],
)
def test_client_rejects_live_mode_or_external_discovery_paths(changed, offline):
    discovery = MockConsole().discovery()
    discovery.update(changed)
    calls = []

    def transport(path):
        calls.append(path)
        assert path == DISCOVERY
        return copy.deepcopy(discovery)

    client = ScriptedClient("A", 1, transport)
    assert client.poll()["compatibility"]["status"] == "blocked"
    assert calls == [DISCOVERY]
    assert not client.cache


def test_download_race_does_not_cache_or_mix_contracts(offline):
    source = MockConsole()
    transport = in_process(create_mock_app(source).test_client())

    def changing(path):
        response = transport(path)
        if "/contracts/" in path:
            source.control("fixture", "compatible")
        return response

    client = ScriptedClient("A", 1, changing)
    assert client.poll()["compatibility"]["status"] == "blocked"
    assert not client.cache
    assert client.state is None


@pytest.mark.parametrize(
    "change",
    [
        {"unit": "mm"},
        {"position": "0.1"},
        {"quality": "new-quality"},
    ],
)
def test_malformed_or_unknown_readings_are_not_interpreted(change, offline):
    transport = in_process(create_mock_app().test_client())

    def changed(path):
        value = transport(path)
        if path.endswith("/state"):
            value["stage"].update(change)
        return value

    client = ScriptedClient("A", 1, changed)
    assert client.poll()["compatibility"]["status"] == "blocked"
    assert client.state is None


def test_local_transport_has_no_arbitrary_origin_or_proxy(monkeypatch, offline):
    monkeypatch.setenv("HTTP_PROXY", "http://100.64.0.1:9999")
    transport = LocalTransport()
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            return b'{"mode":"MOCK"}'

    def open_local(request, **kwargs):
        calls.append(request.full_url)
        assert request.full_url.startswith(ORIGIN + "/")
        return Response()

    monkeypatch.setattr(transport.opener, "open", open_local)
    for path in (
        "http://100.64.0.1:8080/",
        "//100.64.0.1/",
        "/api/console/relay/stage",
        "/mock/control",
    ):
        with pytest.raises(ValueError):
            transport(path)
    transport(DISCOVERY)
    transport("/mock/control", {"action": "pause"})
    assert calls == [ORIGIN + DISCOVERY, ORIGIN + DISCOVERY, ORIGIN + "/mock/control"]
    assert not any(getattr(handler, "proxies", {}) for handler in transport.opener.handlers)


def test_portal_controls_export_and_no_live_command_routes(offline):
    source = MockConsole()
    mock = create_mock_app(source).test_client()
    portal = create_portal_app(in_process(mock)).test_client()
    assert b'data-mode="live"' in portal.get("/").data
    assert b"MOCK" in mock.get("/lab/").data
    status = portal.get("/api/status").get_json()
    assert [row["name"] for row in status["clients"]] == ["A", "B"]
    assert (
        portal.post("/api/control", json={"action": "disconnect", "client": "A"}).status_code == 200
    )
    assert portal.get("/api/status").get_json()["clients"][0]["connected"] is False
    assert (
        portal.post("/api/control", json={"action": "move", "url": "http://100.64.0.1"}).status_code
        == 400
    )
    assert (
        portal.post("/api/control", json={"action": "fixture", "value": "live"}).status_code == 400
    )
    assert portal.post("/api/scenarios", json={}).get_json()["passed"]
    export = portal.get("/api/export")
    assert "attachment" in export.headers["Content-Disposition"]
    assert export.get_json()["results"]["passed"]
    assert export.get_json()["messages"]["B"]
    assert export.get_json()["bundles"]["B"]
    for path in (
        "/lab/api/agent/v1/commands",
        "/lab/api/agent/control",
        "/api/console/relay/stage",
    ):
        assert mock.post(path, json={}).status_code == 404


def test_diagnostics_read_cache_only_and_do_not_expose_private_fields(offline):
    calls = []

    def get(name):
        calls.append(name)
        return {
            "phase": "ready",
            "age_s": 0.2,
            "state": {
                "instance": name + "-instance",
                "reference_revision": 3,
                "reference_valid": True,
                "secret": "private-token",
                "raw": [[1, 2]],
                "motors": [{"id": "bottom", "position_rotations": 0.1, "private": "hidden"}],
            },
        }

    app = Flask(__name__)
    blueprint = Blueprint("agent_connect", __name__)
    register(blueprint, SimpleNamespace(devices=SimpleNamespace(get=get)))
    app.register_blueprint(blueprint)
    client = app.test_client()
    response = client.get("/status")
    body = response.get_json()
    assert len(calls) == len(body["services"]) == 6
    assert body["mode"] == "read-only"
    assert not body["normal_commands"]
    assert all(row["instance_present"] for row in body["services"])
    assert "private" not in json.dumps(body)
    assert "raw" not in json.dumps(body)
    assert client.post("/status", json={}).status_code == 405


def test_fixture_schemas_embed_the_dialect_and_have_no_actions():
    bundle = fixture()
    assert bundle["schemas"]["state"]["$schema"] == bundle["$schema"]
    assert bundle["actions"] == []
