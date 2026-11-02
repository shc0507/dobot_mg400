"""Run a tool-calling client against an existing local Qwen API and MCP gateway."""

import asyncio
from contextlib import contextmanager
from datetime import timedelta
from functools import partial
from threading import Lock

from .guidance import OPERATOR_GUIDANCE

CALL_TIMEOUT = 30.0

GATEWAY_TOOLS = frozenset(
    {
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
)


def gateway_http_client(headers=None, timeout=None, auth=None):
    """Public MCP HTTP factory hook; no proxy, redirects, or transport retries."""
    import httpx

    async def refuse_redirect(response):
        if response.is_redirect:
            raise httpx.HTTPStatusError(
                "Gateway redirects are refused.", request=response.request, response=response
            )

    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout or httpx.Timeout(30),
        auth=auth,
        trust_env=False,
        follow_redirects=False,
        event_hooks={"response": [refuse_redirect]},
    )


def guard_proposal(tool):
    """Permit at most one proposal attempt in this client session, even after failure."""
    forward = tool.forward
    lock = Lock()
    attempted = False

    def once(*args, **kwargs):
        nonlocal attempted
        with lock:
            if attempted:
                raise RuntimeError(
                    "A proposal was already attempted. Do not retry. "
                    "Have a person inspect proposals and current runs on the dashboard."
                )
            attempted = True
        try:
            return forward(*args, **kwargs)
        except Exception:
            raise RuntimeError(
                "Proposal outcome is uncertain. Do not retry. "
                "Have a person inspect proposals and current runs on the dashboard."
            ) from None

    tool.forward = once


class GatewayClient:
    """Use a fresh bounded MCP session for each operation on the stateless gateway."""

    def __init__(self, settings):
        self.settings = settings

    async def _request(self, name, arguments=None):
        import anyio
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        headers = {"Authorization": f"Bearer {self.settings.gateway_token}"}
        with anyio.fail_after(CALL_TIMEOUT):
            async with gateway_http_client(headers=headers) as http:
                async with streamable_http_client(self.settings.gateway_url, http_client=http) as (
                    read,
                    write,
                    _,
                ):
                    async with ClientSession(
                        read, write, read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT)
                    ) as session:
                        await session.initialize()
                        if name is None:
                            return (await session.list_tools()).tools
                        return await session.call_tool(name, arguments)

    def definitions(self):
        try:
            return asyncio.run(self._request(None))
        except Exception:
            raise RuntimeError("Gateway tool discovery unavailable.") from None

    def call(self, name, arguments=None):
        try:
            result = asyncio.run(self._request(name, arguments))
        except Exception:
            raise RuntimeError("Gateway result unavailable. Do not retry a proposal.") from None
        if result.isError:
            raise RuntimeError("Gateway tool failed. Check the state and policy on the dashboard.")
        return result


@contextmanager
def create_agent(settings, *, model=None):
    """Connect only when entered; close model and MCP resources on exit."""
    import httpx
    from mcpadapt.smolagents_adapter import SmolAgentsAdapter
    from smolagents import OpenAIServerModel, ToolCallingAgent

    client = GatewayClient(settings)
    definitions = client.definitions()
    names = [tool.name for tool in definitions]
    if len(names) != len(set(names)) or set(names) != GATEWAY_TOOLS:
        raise ValueError("Expected exactly the gateway read and proposal tools.")
    adapter = SmolAgentsAdapter(structured_output=True)
    tools = [adapter.adapt(partial(client.call, tool.name), tool) for tool in definitions]
    for tool in tools:
        if tool.name == "propose_run":
            guard_proposal(tool)
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=settings.timeout) as http:
        if model is None:
            model = OpenAIServerModel(
                model_id=settings.model_id,
                api_base=settings.model_url,
                api_key=settings.model_key,
                client_kwargs={"http_client": http, "max_retries": 0},
            )
        yield ToolCallingAgent(
            tools=tools,
            model=model,
            add_base_tools=False,
            max_steps=8,
            max_tool_threads=1,
            verbosity_level=0,
            instructions=OPERATOR_GUIDANCE,
        )
