"""In-process MCP and mock HTTP only; no device access or listening sockets."""

import asyncio
import json
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from gateway.app import create_app
from gateway.backend import Backend, configured_endpoints
from orchestrator.recipe import validate
from orchestrator.skills import Registry
from orchestrator.store import RunStore

TOKEN = "gateway-test-credential-000000000000"
EXPECTED = {
    "list_services",
    "get_state",
    "list_recipes",
    "describe_recipe",
    "list_runs",
    "get_run",
    "get_chip",
    "propose_run",
    "get_approval",
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("Gateway test attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture
def env(tmp_path):
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    (recipes / "example.toml").write_text(
        'name="Read example"\nversion=1\n[params]\n[[steps]]\n'
        'skill="sample"\nparams={}\ntimeout_s=1\n'
    )
    with RunStore(tmp_path / "runs") as store:
        yield store, recipes


@asynccontextmanager
async def session(backend, *, caller="configured-agent"):
    app = create_app(backend, token=TOKEN, caller=caller)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), headers={"Authorization": f"Bearer {TOKEN}"}
        ) as http:
            async with streamable_http_client("http://127.0.0.1:8773/mcp", http_client=http) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    yield client


def test_exact_tools_and_audit_before_first_run(env):
    store, recipes = env

    async def exercise():
        backend = Backend(store, recipes, demo=True)
        async with session(backend) as client:
            tools = (await client.list_tools()).tools
            assert {tool.name for tool in tools} == EXPECTED
            assert all(
                (tool.annotations.readOnlyHint is (tool.name != "propose_run"))
                and not tool.annotations.destructiveHint
                for tool in tools
            )
            for name, args in (
                ("list_services", {}),
                ("get_state", {"service": "demo"}),
                ("list_recipes", {}),
                ("describe_recipe", {"id": "example"}),
                ("list_runs", {}),
                ("get_chip", {"id": "missing"}),
                ("get_run", {"id": "missing"}),
            ):
                result = await client.call_tool(name, args)
                assert result.isError is (name == "get_run")
        rows = store.audit_calls()
        assert len(rows) == 7 and {row["tool"] for row in rows} == EXPECTED - {
            "propose_run",
            "get_approval",
        }
        assert all(row["caller"] == "configured-agent" and row["created_at"] > 0 for row in rows)
        assert store.runs() == []
        assert sorted(path.name for path in store.directory.iterdir()) == ["runs.db"]

    asyncio.run(exercise())


def test_invalid_and_mutating_calls_are_audited_without_backend_reads(env, monkeypatch):
    store, recipes = env

    async def exercise():
        backend = Backend(store, recipes)

        async def forbidden(*args):
            pytest.fail("Invalid tool reached backend")

        monkeypatch.setattr(backend, "call", forbidden)
        calls = [
            ("propose_run", {"recipe": "example"}),
            ("stop", {}),
            ("get_state", {"service": "robot", "url": "http://forbidden.invalid"}),
            ("list_runs", {"limit": True}),
            ("get_chip", {"id": "chip", "caller": "impersonated"}),
        ]
        async with session(backend) as client:
            for name, args in calls:
                assert (await client.call_tool(name, args)).isError
        rows = list(reversed(store.audit_calls()))
        assert [(row["tool"], json.loads(row["arguments_json"])) for row in rows] == calls
        assert {row["caller"] for row in rows} == {"configured-agent"}

    asyncio.run(exercise())


def test_audit_failure_prevents_read(env, monkeypatch):
    store, recipes = env

    async def exercise():
        backend = Backend(store, recipes, demo=True)

        def fail(*args):
            raise OSError("private path and secret must not escape")

        monkeypatch.setattr(store, "audit_call", fail)

        async def forbidden(*args):
            pytest.fail("Read occurred without durable audit")

        monkeypatch.setattr(backend, "call", forbidden)
        app = create_app(backend, token=TOKEN)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
                result = await client.post(
                    "http://127.0.0.1:8773/mcp",
                    headers={"Authorization": f"Bearer {TOKEN}"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "list_services", "arguments": {}},
                    },
                )
                assert result.status_code == 503
                assert result.json() == {
                    "error": "Audit unavailable. No tool operation was performed."
                }

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "headers,status",
    [
        ({}, 401),
        ({"Authorization": "Bearer wrong"}, 401),
    ],
)
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_every_http_request_requires_the_gateway_token(env, headers, status, method):
    store, recipes = env

    async def exercise():
        backend = Backend(store, recipes, demo=True)
        app = create_app(backend, token=TOKEN)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
                response = await client.request(
                    method, "http://127.0.0.1:8773/mcp", headers=headers
                )
                assert response.status_code == status
        assert store.audit_calls() == []

    asyncio.run(exercise())


def test_backend_reads_only_fixed_get_routes_and_redacts_credentials(env):
    store, recipes = env
    downstream = "private-downstream-credential-0000000000"
    calls = []

    def respond(request):
        assert request.method == "GET"
        assert request.url.host == "owned-demo.invalid"
        assert request.url.path in {"/mounted/api/state", "/mounted/api/describe"}
        assert request.headers["authorization"] == f"Bearer {downstream}"
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "service": "stage",
                "contract": 1,
                "token": downstream,
                "message": downstream,
                "position": 5,
            },
        )

    async def exercise():
        backend = Backend(
            store,
            recipes,
            endpoints={"stage": ("http://owned-demo.invalid/mounted", downstream)},
            transport=httpx.MockTransport(respond),
        )
        async with session(backend) as client:
            for name, args in (("list_services", {}), ("get_state", {"service": "stage"})):
                result = await client.call_tool(name, args)
                assert not result.isError and downstream not in result.content[0].text
            assert (
                await client.call_tool("get_state", {"service": "http://other.invalid"})
            ).isError
        assert calls == ["/mounted/api/describe", "/mounted/api/describe", "/mounted/api/state"]

    asyncio.run(exercise())


def test_no_redirect_or_cleanup_device_write(env):
    store, recipes = env
    calls = []

    def respond(request):
        calls.append((request.method, str(request.url)))
        return httpx.Response(307, headers={"Location": "http://forbidden.invalid/api/command"})

    async def exercise():
        backend = Backend(
            store,
            recipes,
            endpoints={"stage": ("http://owned-demo.invalid", "")},
            transport=httpx.MockTransport(respond),
        )
        async with session(backend) as client:
            assert (await client.call_tool("get_state", {"service": "stage"})).isError
        assert calls == [("GET", "http://owned-demo.invalid/api/describe")]

    asyncio.run(exercise())


def test_public_audit_and_bounded_chip_reads_survive_reopen(env):
    store, recipes = env
    registry = Registry()

    def sample(ctx, chip_id: str):
        pass

    registry.register("sample", sample)
    recipe = validate(
        {
            "name": "record",
            "version": 1,
            "params": {},
            "steps": [{"skill": "sample", "params": {"chip_id": "chip-7"}, "timeout_s": 1}],
        },
        registry,
    )
    run_id = store.create(recipe)
    store.sample(run_id, 0, "chip-7", {"value": 1})
    store.sample(run_id, 0, "other-chip", {"value": 2})
    store.audit_call("reader", "get_chip", {"id": "chip-7"})
    with RunStore(store.directory, readonly=True) as reader:
        assert reader.audit_calls()[0]["caller"] == "reader"
        assert reader.runs(chip_id="chip-7", limit=1)[0]["id"] == run_id
        assert reader.runs(chip_id="unknown", limit=1) == []
        assert reader.get(run_id, item_limit=1)["truncated"]["samples"]

    async def exercise():
        backend = Backend(store, recipes)
        async with session(backend) as client:
            result = await client.call_tool("get_chip", {"id": "chip-7"})
            assert not result.isError
            records = result.structuredContent["records"]
            assert len(records) == 1
            assert records[0]["samples"][0]["data_json"] == '{"value": 1}'
            assert len(records[0]["samples"]) == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("token", ["", "short", "a" * 24 + "\n", "é" * 24])
def test_gateway_always_requires_valid_long_token(env, token):
    store, recipes = env
    with pytest.raises(ValueError, match="bearer token"):
        create_app(Backend(store, recipes), token=token)


def test_endpoint_settings_are_explicit_and_do_not_generate_tokens():
    assert configured_endpoints({}) == {}
    assert configured_endpoints(
        {
            "MG400_ORCH_STAGE_URL": "http://127.0.0.1:8767/contract",
            "MG400_STAGE_TOKEN": "must-not-be-copied",
        }
    ) == {"stage": ("http://127.0.0.1:8767/contract", "")}


@pytest.mark.parametrize(
    "name,args",
    [
        ("x" * 129, {}),
        ("unknown-é", {}),
        ("", {}),
        ("\ud800", {"value": "\udfff"}),
        (None, {}),
        ({"invalid": True}, {}),
        ("list_services", []),
        ("list_services", "bad"),
        ("list_services", None),
    ],
)
def test_original_invalid_mcp_calls_are_audited_before_sdk_coercion(env, name, args):
    store, recipes = env

    async def exercise():
        backend = Backend(store, recipes, demo=True)
        app = create_app(backend, token=TOKEN, caller="authenticated-caller")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
                payload = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": args},
                    },
                    ensure_ascii=True,
                )
                response = await client.post(
                    "http://127.0.0.1:8773/mcp",
                    content=payload,
                    headers={
                        "Authorization": f"Bearer {TOKEN}",
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                        "X-MG400-Client": "forged-caller",
                    },
                )
                assert response.status_code in {200, 400}
        rows = store.audit_calls()
        assert len(rows) == 1
        assert rows[0]["caller"] == "authenticated-caller"
        assert rows[0]["tool"] == name
        assert json.loads(rows[0]["arguments_json"]) == args
        assert store.runs() == []

    asyncio.run(exercise())


def test_chip_filter_precedes_limits_in_crowded_histories(env):
    store, recipes = env
    registry = Registry()
    registry.register("noop", lambda ctx: None)
    recipe = validate(
        {
            "name": "mixed samples",
            "version": 1,
            "params": {},
            "steps": [{"skill": "noop", "params": {}, "timeout_s": 1}],
        },
        registry,
    )
    run_id = store.create(recipe)
    for index in range(100):
        store.sample(run_id, 0, "other", {"value": index})
        store.event(run_id, "observation", {"value": index}, chip_id="other")
    store.sample(run_id, 0, "target", {"value": "retained"})
    store.event(run_id, "observation", {"value": "retained"}, chip_id="target")

    async def exercise():
        async with session(Backend(store, recipes)) as client:
            result = await client.call_tool("get_chip", {"id": "target"})
            assert not result.isError
            row = result.structuredContent["records"][0]
            assert len(row["samples"]) == len(row["events"]) == 1
            assert row["samples"][0]["chip_id"] == "target"
            assert not any(row["truncated"].values())

    asyncio.run(exercise())


@pytest.mark.parametrize("version", [None, True, 2, "1"])
def test_incompatible_contract_never_reads_state(env, version):
    store, recipes = env
    calls = []

    def respond(request):
        calls.append(request.url.path)
        assert request.method == "GET" and request.url.path == "/api/describe"
        return httpx.Response(200, json={"service": "stage", "contract": version})

    async def exercise():
        backend = Backend(
            store,
            recipes,
            endpoints={"stage": ("http://owned.invalid", "")},
            transport=httpx.MockTransport(respond),
        )
        async with session(backend) as client:
            result = await client.call_tool("list_services", {})
            assert result.structuredContent == {"services": [{"id": "stage", "available": False}]}
            assert (await client.call_tool("get_state", {"service": "stage"})).isError
        assert calls == ["/api/describe", "/api/describe"]

    asyncio.run(exercise())


def test_explicit_loopback_bind_matches_sdk_host_rule(env):
    store, recipes = env

    async def exercise():
        backend = Backend(store, recipes, demo=True)
        app = create_app(backend, token=TOKEN, bind="127.0.0.2")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), headers={"Authorization": f"Bearer {TOKEN}"}
            ) as http:
                async with streamable_http_client(
                    "http://127.0.0.2:8773/mcp", http_client=http
                ) as (read, write, _):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        assert not (await client.call_tool("list_services", {})).isError

    asyncio.run(exercise())
