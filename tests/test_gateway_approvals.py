"""MCP proposals use an in-process Flask adapter; no port is opened."""

import asyncio
import json

import httpx
import pytest
from test_approvals import GATEWAY, OPERATOR, headers
from test_approvals import rig as rig
from test_gateway import session

from gateway.backend import Backend


def test_mcp_proposal_is_audited_and_never_starts_until_operator_decides(rig):
    service, browser, _ = rig
    requests = []

    def transport(request):
        requests.append((request.method, request.url.path))
        assert service.store.audit_calls()[0]["tool"] in {"propose_run", "get_approval"}
        assert request.headers["authorization"] == "Bearer " + GATEWAY
        result = browser.open(
            request.url.path,
            method=request.method,
            headers=dict(request.headers),
            data=request.content,
        )
        return httpx.Response(result.status_code, json=result.json)

    async def exercise():
        backend = Backend(
            service.store,
            service.catalog.directory,
            proposal=("http://127.0.0.1:8772", GATEWAY, "agent"),
            transport=httpx.MockTransport(transport),
        )
        async with session(backend) as mcp:
            result = await mcp.call_tool(
                "propose_run", {"recipe": "wait", "params": {}, "reason": "Review"}
            )
            assert not result.isError
            record = result.structuredContent
            assert record["requester"] == "test-agent"
            assert record["decision"] == "pending"
            assert service.current is None and service.lease.snapshot()["holder"] is None
            result = await mcp.call_tool("get_approval", {"id": record["id"]})
            assert result.structuredContent["decision"] == "pending"
            for name in ("approve", "approve_run", "run", "stop", "heartbeat"):
                assert (await mcp.call_tool(name, {})).isError
            response = browser.post(
                "/api/approvals/" + record["id"] + "/decision",
                headers=headers(OPERATOR),
                json={"decision": "reject", "present": False, "fingerprint": record["fingerprint"]},
            )
            assert response.status_code == 200
            result = await mcp.call_tool("get_approval", {"id": record["id"]})
            assert result.structuredContent["decision"] == "rejected"

    asyncio.run(exercise())
    assert [method for method, _ in requests] == ["POST", "GET", "GET"]
    assert all(path.startswith("/api/proposals") for _, path in requests)
    assert service.store.runs() == []


@pytest.mark.parametrize(
    "url",
    [
        "http://hardware.invalid",
        "https://127.0.0.1",
        "http://user:pass@127.0.0.1",
        "http://127.0.0.1/a?x=y",
        "http://127.0.0.1/#x",
        "http://127.0.0.1/a/../runs",
        "http://127.0.0.1/%2e%2e",
        "http://127.0.0.1//runs",
        "http://127.0.0.1/a\\b",
    ],
)
def test_proposal_connection_is_fixed_direct_loopback(rig, url):
    service, _, _ = rig
    with pytest.raises(ValueError):
        Backend(service.store, service.catalog.directory, proposal=(url, GATEWAY, "agent"))


def test_proposal_redirect_failure_and_path_injection_never_reach_other_routes(rig):
    service, _, _ = rig
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(307, headers={"location": "http://hardware.invalid/api/command"})

    async def exercise():
        backend = Backend(
            service.store,
            service.catalog.directory,
            proposal=("http://127.0.0.1:8772/lab", GATEWAY, "agent"),
            transport=httpx.MockTransport(transport),
        )
        async with session(backend) as mcp:
            assert (
                await mcp.call_tool(
                    "propose_run", {"recipe": "wait", "params": {}, "reason": "Review"}
                )
            ).isError
            for identifier in ("../runs", "id/decision", "%2e%2e", "?x=y"):
                assert (await mcp.call_tool("get_approval", {"id": identifier})).isError

    asyncio.run(exercise())
    assert len(calls) == 1 and calls[0].url.path == "/lab/api/proposals"
    assert json.loads(calls[0].content) == {"recipe": "wait", "params": {}, "reason": "Review"}


def test_mcp_audit_failure_cannot_even_submit_a_proposal(rig, monkeypatch):
    service, _, _ = rig

    def transport(request):
        pytest.fail("Unaudited proposal reached orchestrator")

    def broken(*args):
        raise OSError("audit unavailable")

    monkeypatch.setattr(service.store, "audit_call", broken)

    async def exercise():
        backend = Backend(
            service.store,
            service.catalog.directory,
            proposal=("http://127.0.0.1:8772", GATEWAY, "agent"),
            transport=httpx.MockTransport(transport),
        )
        with pytest.raises(ExceptionGroup):
            async with session(backend) as mcp:
                await mcp.call_tool(
                    "propose_run", {"recipe": "wait", "params": {}, "reason": "Review"}
                )

    asyncio.run(exercise())
    assert service.approvals.records == {}
