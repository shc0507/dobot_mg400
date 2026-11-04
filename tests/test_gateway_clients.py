"""Fake model and in-process HTTP only; never connect to a device or a server."""

import asyncio
import importlib
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from smolagents import Model
from smolagents.models import ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction

from gateway.app import definitions
from gateway.clients import smolagents_example
from gateway.clients.config import Settings
from gateway.clients.guidance import OPERATOR_GUIDANCE
from gateway.clients.smolagents_example import create_agent, gateway_http_client, guard_proposal

TOKEN = "gateway-client-test-token-000000000000"
MODEL_KEY = "model-only-test-credential"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("Client test attempted a real network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture
def settings():
    return Settings.from_env(
        {
            "MG400_GATEWAY_TOKEN": TOKEN,
            "QUMUS_DEFAULT_MODEL_ID": "Qwen/Qwen3-8B",
            "QUMUS_API_KEY": MODEL_KEY,
            "MG400_ORCH_TOKEN": "operator-secret-must-not-be-read",
            "MG400_STAGE_TOKEN": "instrument-secret-must-not-be-read",
        }
    )


def test_settings_keep_credentials_separate_and_read_only_explicit_names(settings):
    assert settings.gateway_url == "http://127.0.0.1:8773/mcp"
    assert settings.model_url == "http://127.0.0.1:8000/v1"
    assert settings.gateway_token == TOKEN and settings.model_key == MODEL_KEY
    assert TOKEN not in repr(settings) and MODEL_KEY not in repr(settings)
    assert "operator-secret" not in repr(settings)


@pytest.mark.parametrize(
    "key,value",
    [
        ("MG400_GATEWAY_URL", "http://device.invalid:8773/mcp"),
        ("MG400_GATEWAY_URL", "http://127.0.0.1:8773/api/command"),
        ("MG400_GATEWAY_URL", "http://user:secret@127.0.0.1:8773/mcp"),
        ("MG400_GATEWAY_URL", "http://127.0.0.1:8773/mcp?token=secret"),
        ("MG400_GATEWAY_URL", "http://127.0.0.1:8773/mcp#fragment"),
        ("MG400_GATEWAY_URL", "http://127.0.0.1/mcp"),
        ("MG400_GATEWAY_URL", "http://127.0.0.1:bad/mcp"),
        ("MG400_GATEWAY_URL", "http://127.0.0.1:\n8773/mcp"),
        ("QUMUS_API_BASE", "http://provider.invalid:8000/v1"),
        ("QUMUS_API_BASE", "http://127.0.0.1:8000/v1?key=secret"),
        ("QUMUS_API_KEY", TOKEN),
        ("QUMUS_LLM_TIMEOUT", "NaN"),
        ("QUMUS_LLM_TIMEOUT", "inf"),
        ("QUMUS_LLM_TIMEOUT", "0"),
        ("QUMUS_LLM_TIMEOUT", "301"),
        ("QUMUS_LLM_RETRIES", "1"),
        ("MG400_GATEWAY_TOKEN", "short"),
        ("MG400_GATEWAY_TOKEN", TOKEN + "\n"),
        ("QUMUS_DEFAULT_MODEL_ID", ""),
    ],
)
def test_invalid_settings_fail_without_disclosing_values(key, value):
    env = {"MG400_GATEWAY_TOKEN": TOKEN, "QUMUS_DEFAULT_MODEL_ID": "qwen", key: value}
    with pytest.raises(ValueError) as exc:
        Settings.from_env(env)
    assert "secret" not in str(exc.value) and TOKEN not in str(exc.value)


def test_claude_configuration_contains_only_gateway_placeholder():
    config = json.loads((ROOT / "src/gateway/clients/claude-code.mcp.json").read_text())
    assert config == {
        "mcpServers": {
            "mg400-gateway": {
                "type": "http",
                "url": "http://127.0.0.1:8773/mcp",
                "headers": {"Authorization": "Bearer ${MG400_GATEWAY_TOKEN}"},
            }
        }
    }


def test_import_opens_no_client_or_thread(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("Import initialized a connection or worker thread")

    import threading

    monkeypatch.setattr(threading.Thread, "start", refuse)
    monkeypatch.setattr(httpx, "Client", refuse)
    monkeypatch.setattr(httpx, "AsyncClient", refuse)
    for name in (
        "gateway.clients",
        "gateway.clients.config",
        "gateway.clients.guidance",
        "gateway.clients.smolagents_example",
        "gateway.clients.__main__",
    ):
        importlib.reload(importlib.import_module(name))


def test_gateway_factory_disables_proxy_and_redirects(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:9000")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:9000")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:9000")

    async def exercise():
        async with gateway_http_client({"Authorization": f"Bearer {TOKEN}"}) as client:
            assert not client.trust_env and not client.follow_redirects
            assert client.headers["Authorization"] == f"Bearer {TOKEN}"
            assert client._mounts == {}

    asyncio.run(exercise())


def test_gateway_redirect_is_refused_before_sdk_can_follow(settings, monkeypatch):
    requests = []
    original = httpx.AsyncClient

    def redirect(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "/other"})

    def factory(**kwargs):
        return original(transport=httpx.MockTransport(redirect), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    with pytest.raises(RuntimeError, match="discovery unavailable") as exc:
        smolagents_example.GatewayClient(settings).definitions()
    assert TOKEN not in str(exc.value)
    assert len(requests) == 1 and requests[0].url.path == "/mcp"


def test_call_deadline_closes_hung_transport(settings, monkeypatch):
    clients = []
    entered = []

    async def hung(request):
        entered.append(request)
        await asyncio.Event().wait()

    def factory(**kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(hung), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(smolagents_example, "CALL_TIMEOUT", 0.05)
    monkeypatch.setattr(smolagents_example, "gateway_http_client", factory)
    tool = SimpleNamespace(
        forward=lambda **kwargs: smolagents_example.GatewayClient(settings).call(
            "propose_run", kwargs
        )
    )
    guard_proposal(tool)
    with pytest.raises(RuntimeError, match="outcome is uncertain") as exc:
        tool.forward(recipe="sample", params={}, reason="Inspect")
    assert TOKEN not in str(exc.value)
    with pytest.raises(RuntimeError, match="already attempted"):
        tool.forward(recipe="sample", params={}, reason="Retry")
    assert len(entered) == 1 and clients[0].is_closed


@pytest.mark.parametrize("fails", [False, True])
def test_proposal_attempt_is_never_retried_even_after_lost_response(fails):
    calls = []

    def forward(**kwargs):
        calls.append(kwargs)
        if fails:
            raise TimeoutError("private endpoint and credential")
        return {"id": "proposal", "decision": "pending"}

    tool = SimpleNamespace(forward=forward)
    guard_proposal(tool)
    if fails:
        with pytest.raises(RuntimeError, match="outcome is uncertain") as exc:
            tool.forward(recipe="sample", params={}, reason="Inspect chip")
        assert "private" not in str(exc.value)
    else:
        assert (
            tool.forward(recipe="sample", params={}, reason="Inspect chip")["decision"] == "pending"
        )
    with pytest.raises(RuntimeError, match="already attempted"):
        tool.forward(recipe="sample", params={}, reason="Retry")
    assert len(calls) == 1


class ScriptedModel(Model):
    def __init__(self, calls):
        super().__init__(model_id="fake-local-qwen")
        self.calls = iter(calls)
        self.messages = []

    def generate(self, messages, **kwargs):
        self.messages.append(messages)
        name, arguments = next(self.calls)
        return ChatMessage(
            role="assistant",
            content=None,
            tool_calls=[
                ChatMessageToolCall(
                    id=str(len(self.messages)),
                    type="function",
                    function=ChatMessageToolCallFunction(name=name, arguments=arguments),
                )
            ],
        )


def install_fake_mcp(monkeypatch, *, tool_names=None, proposal_failure=False):
    calls = []
    requests = []
    closed = []

    def respond(request):
        requests.append(request)
        assert request.url.host == "127.0.0.1" and request.url.path == "/mcp"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert MODEL_KEY not in str(request.headers)
        if request.method == "GET":
            return httpx.Response(405)
        if request.method == "DELETE":
            return httpx.Response(200)
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        method = body["method"]
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-gateway", "version": "1"},
            }
        elif method == "tools/list":
            tools = definitions()
            if tool_names is not None:
                tools = [tool for tool in tools if tool.name in tool_names]
            result = {"tools": [tool.model_dump(mode="json") for tool in tools]}
        elif method == "tools/call":
            params = body["params"]
            calls.append(params)
            name = params["name"]
            if name == "propose_run" and proposal_failure:
                raise httpx.ReadTimeout("lost proposal reply")
            data = {"id": "proposal", "decision": "pending"}
            if name == "list_recipes":
                data = {"recipes": [{"id": "sample"}]}
            result = {
                "isError": False,
                "content": [{"type": "text", "text": json.dumps(data)}],
                "structuredContent": data,
            }
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    def factory(headers=None, timeout=None, auth=None):
        client = httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            auth=auth,
            transport=httpx.MockTransport(respond),
            trust_env=False,
            follow_redirects=False,
        )
        closed.append(client)
        return client

    monkeypatch.setattr(smolagents_example, "gateway_http_client", factory)
    return calls, requests, closed


def test_real_mcp_adapter_and_agent_propose_but_cannot_approve(settings, monkeypatch):
    calls, requests, closed = install_fake_mcp(monkeypatch)
    model = ScriptedModel(
        [
            ("list_recipes", {}),
            ("propose_run", {"recipe": "sample", "params": {}, "reason": "Inspect chip"}),
            ("get_approval", {"id": "proposal"}),
            (
                "final_answer",
                {"answer": "Proposal pending; a person must approve on the dashboard."},
            ),
        ]
    )
    with create_agent(settings, model=model) as agent:
        assert set(agent.tools) == smolagents_example.GATEWAY_TOOLS | {"final_answer"}
        result = agent.run("Propose sample for review.")
        assert "pending" in result
    assert [call["name"] for call in calls] == ["list_recipes", "propose_run", "get_approval"]
    assert OPERATOR_GUIDANCE in model.messages[0][0].content[0]["text"]
    assert requests and closed and all(client.is_closed for client in closed)


def test_model_cannot_repeat_proposal_after_uncertain_outcome(settings, monkeypatch):
    calls, _, _ = install_fake_mcp(monkeypatch, proposal_failure=True)
    proposal = ("propose_run", {"recipe": "sample", "params": {}, "reason": "Inspect chip"})
    model = ScriptedModel(
        [proposal, proposal, ("final_answer", {"answer": "Uncertain; inspect the dashboard."})]
    )
    with create_agent(settings, model=model) as agent:
        assert "Uncertain" in agent.run("Propose sample for review.")
    assert [call["name"] for call in calls] == ["propose_run"]
    assert "already attempted" in str(model.messages[-1])


def test_unexpected_server_tool_set_is_rejected(settings, monkeypatch):
    install_fake_mcp(monkeypatch, tool_names={"list_recipes"})
    with pytest.raises(ValueError, match="exactly the gateway"):
        with create_agent(settings, model=ScriptedModel([])):
            pytest.fail("Unexpected tool set accepted")


def test_model_http_client_has_separate_key_and_no_retry_or_proxy(settings, monkeypatch):
    import smolagents

    install_fake_mcp(monkeypatch)
    captured = {}

    def model_factory(**kwargs):
        captured.update(kwargs)
        return ScriptedModel([])

    monkeypatch.setattr(smolagents, "OpenAIServerModel", model_factory)
    with create_agent(settings):
        http = captured["client_kwargs"]["http_client"]
        assert not http.trust_env and not http.follow_redirects
        assert captured["client_kwargs"]["max_retries"] == 0
        assert captured["retry"] is False
        assert captured["api_key"] == MODEL_KEY and TOKEN not in str(captured)
        assert captured["api_base"] == "http://127.0.0.1:8000/v1"
    assert http.is_closed


class GuidanceBackend:
    def __init__(self):
        self.store = self
        self.calls = []
        self.closed = False

    def audit_call(self, caller, name, args):
        self.calls.append((caller, name, args))
        return 1

    async def call(self, name, args):
        return {"read": name}

    async def close(self):
        self.closed = True


def test_guidance_prompt_and_resource_share_access_and_leave_tools_unchanged():
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.exceptions import McpError

    from gateway.app import create_app
    from gateway.clients.guidance import GUIDANCE_NAME, GUIDANCE_URI

    async def exercise():
        backend = GuidanceBackend()
        app = create_app(backend, token=TOKEN)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                headers={"Authorization": f"Bearer {TOKEN}"},
            ) as http:
                async with streamable_http_client(
                    "http://127.0.0.1:8773/mcp", http_client=http
                ) as (read, write, _):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        prompts = (await client.list_prompts()).prompts
                        resources = (await client.list_resources()).resources
                        assert [prompt.name for prompt in prompts] == [GUIDANCE_NAME]
                        assert prompts[0].arguments == []
                        assert [str(resource.uri) for resource in resources] == [GUIDANCE_URI]
                        prompt = await client.get_prompt(GUIDANCE_NAME)
                        resource = await client.read_resource(GUIDANCE_URI)
                        assert prompt.messages[0].content.text == OPERATOR_GUIDANCE
                        assert resource.contents[0].text == OPERATOR_GUIDANCE
                        assert resource.contents[0].mimeType == "text/plain"
                        assert backend.calls == []
                        assert {tool.name for tool in (await client.list_tools()).tools} == (
                            smolagents_example.GATEWAY_TOOLS
                        )
                        for name, args in [("unknown", {}), (GUIDANCE_NAME, {"action": "approve"})]:
                            with pytest.raises(McpError):
                                await client.get_prompt(name, args)
                        with pytest.raises(McpError):
                            await client.read_resource("file:///private/credentials")
                        assert backend.calls == []
                        assert not (await client.call_tool("list_recipes", {})).isError
                        assert backend.calls == [("gateway-agent", "list_recipes", {})]
        assert backend.closed

    asyncio.run(exercise())


@pytest.mark.parametrize("method", ["prompts/get", "resources/read"])
@pytest.mark.parametrize(
    "headers,status",
    [
        ({}, 401),
        ({"Authorization": "Bearer wrong-token"}, 401),
    ],
)
def test_guidance_endpoints_need_the_gateway_token(method, headers, status):
    from gateway.app import create_app

    async def exercise():
        backend = GuidanceBackend()
        app = create_app(backend, token=TOKEN)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
                response = await http.post(
                    "http://127.0.0.1:8773/mcp",
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {}},
                )
                assert response.status_code == status
                assert backend.calls == []

    asyncio.run(exercise())


def test_ambient_custom_headers_are_refused_before_any_connection(settings, monkeypatch):
    monkeypatch.setenv(
        "OPENAI_CUSTOM_HEADERS", f"Authorization: Bearer {TOKEN}\nX-Private: operator-secret"
    )

    def refuse(*args, **kwargs):
        pytest.fail("Client connected with inherited custom headers")

    monkeypatch.setattr(smolagents_example.GatewayClient, "definitions", refuse)
    with pytest.raises(ValueError, match="Unset OPENAI_CUSTOM_HEADERS") as exc:
        with create_agent(settings):
            pytest.fail("Ambient custom headers accepted")
    assert TOKEN not in str(exc.value) and "operator-secret" not in str(exc.value)


@pytest.mark.parametrize("status", [200, 307, 429, 503])
def test_real_model_sdk_keeps_credentials_local_and_never_retries(settings, monkeypatch, status):
    # Load the SDK's HTTP client subclass before replacing the constructor.
    import openai

    assert openai.OpenAI
    install_fake_mcp(monkeypatch)
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    ambient = "ambient-operator-instrument-credential"
    for name in ("OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "OPENAI_ADMIN_KEY", "OPENAI_WEBHOOK_SECRET"):
        monkeypatch.setenv(name, ambient)
    monkeypatch.setenv("OPENAI_API_KEY", TOKEN)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://outside.invalid/v1")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:9000")
    requests = []
    clients = []
    original = httpx.Client

    def respond(request):
        requests.append(request)
        assert str(request.url) == "http://127.0.0.1:8000/v1/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {MODEL_KEY}"
        wire = str(request.headers) + request.content.decode()
        assert TOKEN not in wire and ambient not in wire
        if status == 307:
            return httpx.Response(status, headers={"Location": "/other"})
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "Provider unavailable"}})
        return httpx.Response(
            200,
            json={
                "id": "fake-completion",
                "object": "chat.completion",
                "created": 0,
                "model": "Qwen/Qwen3-8B",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Ready to read."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    class ModelHTTP(original):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(respond), **kwargs)
            clients.append(self)

    monkeypatch.setattr(httpx, "Client", ModelHTTP)
    with create_agent(settings) as agent:
        if status == 200:
            assert agent.model.generate(
                [ChatMessage(role="user", content="Read status.")]
            ).content == ("Ready to read.")
        else:
            with pytest.raises(Exception):
                agent.model.generate([ChatMessage(role="user", content="Read status.")])
    assert len(requests) == 1
    assert clients[0].is_closed
