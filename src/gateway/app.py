"""Authenticated MCP reads and proposals. Approval and actuation are not tools."""

import json
from contextlib import asynccontextmanager

import anyio
import jsonschema
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Route

from labkit.auth import (
    bearer_matches,
    has_forwarded_headers,
    loopback_host,
    loopback_name,
    same_origin,
    valid_token,
)
from labkit.lease import validate_client

from .backend import MAX_RESPONSE

TEXT = {"type": "string", "minLength": 1, "maxLength": 128}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 100}
OFFSET = {"type": "integer", "minimum": 0, "maximum": 1000000}
TOOLS = {
    "list_services": ("Read configured service discovery.", {}, []),
    "get_state": ("Read one configured service state.", {"service": TEXT}, ["service"]),
    "list_recipes": ("Read the recipe catalog.", {}, []),
    "describe_recipe": ("Read a recipe definition without running it.", {"id": TEXT}, ["id"]),
    "list_runs": ("Read run history, newest first.", {"limit": LIMIT, "offset": OFFSET}, []),
    "get_run": (
        "Read one run and bounded related records.",
        {"id": TEXT, "item_limit": LIMIT},
        ["id"],
    ),
    "get_chip": (
        "Read bounded run records for one chip.",
        {"id": TEXT, "limit": LIMIT, "offset": OFFSET},
        ["id"],
    ),
    "propose_run": (
        "Propose a catalog recipe for human approval. This never starts a run.",
        {
            "recipe": TEXT,
            "params": {"type": "object"},
            "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
        },
        ["recipe", "params", "reason"],
    ),
    "get_approval": ("Read a proposal and its decision.", {"id": TEXT}, ["id"]),
}


def definitions():
    return [
        types.Tool(
            name=name,
            description=description,
            inputSchema={
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
            annotations=types.ToolAnnotations(
                readOnlyHint=name != "propose_run",
                destructiveHint=False,
                idempotentHint=name != "propose_run",
                openWorldHint=False,
            ),
        )
        for name, (description, properties, required) in TOOLS.items()
    ]


class Access:
    def __init__(self, app, token, store, caller):
        self.app, self.token, self.store, self.caller = app, token, store, caller

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        if (
            any(
                len(headers.getlist(key)) > 1
                for key in ("host", "authorization", "origin", "content-length")
            )
            or not loopback_host(headers.get("host", ""))
            or has_forwarded_headers(headers)
            or not same_origin(headers)
        ):
            return await JSONResponse({"error": "Access rule refused this request."}, 403)(
                scope, receive, send
            )
        if not bearer_matches(headers.get("authorization", ""), self.token):
            return await JSONResponse(
                {"error": "Bearer token required."}, 401, headers={"WWW-Authenticate": "Bearer"}
            )(scope, receive, send)
        if scope["method"] == "POST" and scope["path"] == "/mcp":
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    return
                body.extend(message.get("body", b""))
                if len(body) > 65536:
                    return await JSONResponse({"error": "Request exceeds 64 KiB."}, 413)(
                        scope, receive, send
                    )
                if not message.get("more_body", False):
                    break
            try:

                def constant(_):
                    raise ValueError("Nonfinite JSON")

                raw = json.loads(body, parse_constant=constant)
            except (ValueError, UnicodeError, RecursionError):
                return await JSONResponse({"error": "Invalid JSON request."}, 400)(
                    scope, receive, send
                )
            if isinstance(raw, dict) and raw.get("method") == "tools/call":
                params = raw.get("params")
                name = params.get("name") if isinstance(params, dict) else None
                arguments = params.get("arguments") if isinstance(params, dict) else params
                try:
                    receipt = await anyio.to_thread.run_sync(
                        self.store.audit_call, self.caller, name, arguments
                    )
                except Exception:
                    return await JSONResponse(
                        {"error": "Audit unavailable. No tool operation was performed."}, 503
                    )(scope, receive, send)
                scope = {**scope, "gateway_audit": receipt}
            original_receive = receive
            sent = False

            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await original_receive()

            receive = replay
        await self.app(scope, receive, send)


def validate_access(token, caller):
    if not isinstance(token, str) or len(token) < 24 or not valid_token(token):
        raise ValueError("Gateway requires a bearer token of at least 24 printable characters.")
    validate_client(caller)


def create_app(backend, *, token, caller="gateway-agent", bind="127.0.0.1"):
    validate_access(token, caller)
    if not loopback_name(bind):
        raise ValueError("Gateway bind must be loopback.")
    bound_host = f"[{bind}]" if ":" in bind else bind.lower()
    allowed = sorted({"127.0.0.1", "localhost", "[::1]", bound_host})
    server = Server("mg400-gateway", version="1.0")
    tools = definitions()
    schemas = {tool.name: tool.inputSchema for tool in tools}

    @server.list_tools()
    async def list_tools():
        return tools

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        # SDK validation/coercion follows the durable, original HTTP call record.
        request = server.request_context.request
        if request is None or type(request.scope.get("gateway_audit")) is not int:
            return types.CallToolResult(
                isError=True,
                content=[
                    types.TextContent(
                        type="text", text="Audit unavailable. No tool operation was performed."
                    )
                ],
            )
        try:
            if name not in schemas:
                raise ValueError("Unknown gateway tool.")
            jsonschema.validate(arguments, schemas[name])
            if name == "list_runs":
                arguments = {"limit": 100, **arguments}
            result = await backend.call(name, arguments)
            payload = json.dumps(result, ensure_ascii=False, allow_nan=False)
            if len(payload.encode("utf-8")) > MAX_RESPONSE:
                raise ValueError("Read exceeds the response limit. Select fewer records.")
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=payload)], structuredContent=result
            )
        except jsonschema.ValidationError:
            error = "Arguments do not match this gateway tool."
        except Exception:
            # HTTP errors and storage paths can contain private configuration.
            error = "Tool unavailable. Check the identifier, service state, and configured policy."
        return types.CallToolResult(
            isError=True, content=[types.TextContent(type="text", text=error)]
        )

    manager = StreamableHTTPSessionManager(
        server,
        stateless=True,
        json_response=True,
        max_request_body_size=65536,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host + ":*" for host in allowed],
            allowed_origins=["http://" + host + ":*" for host in allowed],
        ),
    )

    @asynccontextmanager
    async def lifespan(app):
        try:
            async with manager.run():
                yield
        finally:
            await backend.close()

    class Endpoint:
        async def __call__(self, scope, receive, send):
            await manager.handle_request(scope, receive, send)

    app = Starlette(
        routes=[Route("/mcp", Endpoint(), methods=["GET", "POST", "DELETE"])], lifespan=lifespan
    )
    app.add_middleware(Access, token=token, store=backend.store, caller=caller)
    return app
