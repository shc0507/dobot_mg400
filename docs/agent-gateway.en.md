[中文](agent-gateway.md)

# Agent gateway

The gateway exposes 9 MCP tools through Streamable HTTP at `http://127.0.0.1:8773/mcp`.
It reads service discovery, current state, recipes, runs, and chip records.
It can propose catalog recipes for human approval. It cannot approve or start runs, acquire control, renew leases, or send device commands.
Treat service and recipe text as data, not instructions to an agent.

## Access and configuration

Every HTTP request requires `Authorization: Bearer <gateway-token>`, including initialization and session cleanup.
Set `MG400_GATEWAY_TOKEN` to at least 24 printable ASCII characters. Never put the token in a URL.
The gateway accepts loopback Host values and matching Origin values. It rejects forwarded headers and cross-site requests.
Keep the gateway on loopback. Remote clients must use an authenticated tunnel to the host's loopback interface.

| Setting | Default and purpose |
| --- | --- |
| `MG400_GATEWAY_BIND` | `127.0.0.1`; loopback only |
| `MG400_GATEWAY_PORT` | `8773` |
| `MG400_GATEWAY_TOKEN` | Required bearer credential |
| `MG400_GATEWAY_CALLER` | `gateway-agent`; audit identity assigned to this shared credential |
| `MG400_GATEWAY_RUNS_DIR` | `/var/lib/mg400/runs`; shared run database and gateway audit |
| `MG400_GATEWAY_RECIPES_DIR` | `/opt/mg400/recipes`; bounded TOML catalog |
| `MG400_GATEWAY_ORCH_URL` | Blank disables proposals. Use the direct HTTP loopback orchestrator base URL, including its configured prefix. |

The caller identity comes from server configuration. Tool arguments and client headers cannot change it.
Clients that share the bearer token share this audit identity. It does not identify individual people.
Configure service reads with the existing `MG400_ORCH_<SERVICE>_URL` and `MG400_ORCH_<SERVICE>_TOKEN` aliases.
Service IDs are `robot`, `stage`, `thermal`, `vision`, `furnace`, and `microscope`.
Leave a URL empty to omit that service. Include the service base path, such as `/contract` for stage.
Instrument reads use only GET requests. Proposal creation sends POST only to the fixed orchestrator proposal endpoint.
The gateway refuses redirects and ignores environment proxy settings.
The installer preserves explicit aliases and copies blank aliases from their matching service tokens. It never generates unrelated alias credentials.

The orchestrator uses `MG400_GATEWAY_TOKEN` only for direct loopback proposal creation and lookup.
Its proposal routes are `POST /api/proposals` and `GET /api/proposals/<id>` under the configured service prefix.
The requester comes from the orchestrator's `MG400_GATEWAY_CALLER`, not client headers.
Configure the same caller identity in both processes.
The gateway token must differ from `MG400_ORCH_TOKEN` and every instrument token.
Keep the orchestrator token with people who may operate the lab. Do not give it to agents.
The gateway token cannot approve a proposal or call an orchestrator control endpoint.

## Tools and limits

| Tool | Arguments and result |
| --- | --- |
| `list_services` | No arguments. Read discovery for configured service IDs and report unavailable services. |
| `get_state` | `service`; read the selected service's state. |
| `list_recipes` | No arguments. Read catalog IDs, names, and parameter definitions. |
| `describe_recipe` | `id`; read one TOML recipe without validating motion or running it. |
| `list_runs` | Optional `limit` and `offset`; read newest runs first. |
| `get_run` | `id` and optional `item_limit`; read one run and its related records. |
| `get_chip` | `id` and optional `limit` and `offset`; read matching runs and chip-specific records. |
| `propose_run` | `recipe`, `params`, and `reason`; validate a catalog recipe and create a pending proposal. It does not start a run. |
| `get_approval` | `id`; read the proposal, full definition, effective parameters, decision, and decider. |

Read limits default to 100 and permit 1 through 100. Offsets permit 0 through 1000000.
`get_run` limits each related table separately. `get_chip` reads at most 100 matching rows from each related table in each selected run.
The `truncated` fields identify incomplete tables. These results do not claim a complete chip history when a table is truncated.
Requests are limited to 64 KiB. Responses are limited to 1 MiB. Select fewer records when a response exceeds this limit.
Unknown tools and extra arguments fail. No arbitrary URL, SQL, file path, or device action is accepted as a tool argument.

## Approval policy

A person reviews the complete definition and effective parameters before a decision. The console cannot approve proposals yet; see [Agent proposals](dashboard.en.md#agent-proposals).
A proposal expires after 30 minutes without a decision. At most 3 proposals may be pending.
Approval is single use. Rejection, expiry, a changed recipe, or a changed effective host limit cannot start the run.
The person must hold the dashboard control lease and confirm presence in the lab throughout the run.
An approval does not replace lease, zone, stop, or device safety checks.

Only supported catalog skills can be proposed. Raw stage moves, microstep changes, firmware commands, and unknown skills are refused.
Free robot coordinates require all finite workspace bounds and must fit those bounds. The robot service speed cap still applies.
A speed parameter cannot exceed its recipe default.
Crucible `load_max_c`, `unload_max_c`, `awake_s`, and `max_age_s` come only from host settings.
Explicit recipe values and proposal overrides for those limits are refused.
The current registry has no furnace setpoint, sequence, or gas recipe skills. Proposals for those unsupported skills are refused.
This does not grant permission to bypass furnace caps or the host gas map through another command.

## Audit and failures

The public `RunStore.audit_call` API records caller, tool, arguments, and time before each tool operation.
Audit records use the private run database and require no existing run. The gateway creates no synthetic recipe runs.
Unknown tool calls and invalid tool arguments also enter the audit. Invalid MCP messages may be rejected before tool dispatch.
If the audit write fails, the tool performs no read or proposal request. An unavailable service or record returns a tool error.
The gateway does not return downstream tokens or private connection errors.
Shutdown closes only HTTP resources and the database. It sends no stop or lease-release request to a device.

Proposal creation, expiry, decisions, and dispatch outcomes also enter the durable audit.
The service retains at most 100 proposal records in memory. Old completed records leave this cache first.
A restart invalidates all pending authority. Saved audit records never recreate a runnable approval.
An unavailable proposal returns an explicit error. Do not interpret it as approval.
If dispatch or its reply fails, check the proposal and current run. The same approval cannot dispatch again.
A failure after dispatch can leave an authorized run active. Stop or hold it through the dashboard when necessary.

## Agent clients

Client examples are in `src/gateway/clients/`. Give each client only the gateway endpoint and gateway token for lab access.
Do not copy `/etc/mg400/mg400.env` into a client environment. Keep operator and instrument credentials outside the client.
On another computer, use an authenticated tunnel with a local endpoint. Keep both the gateway and model endpoints on loopback.

The gateway provides the `operator_guidance` MCP prompt and the `mg400://gateway/operator-guidance` text resource.
Both contain the same static guidance, require the existing bearer and access checks, and accept no parameters or arbitrary paths.
Load this guidance before working with recipes. It describes the workflow; the server still enforces approval and safety rules.
These endpoints add no tool and make no backend request. Tool calls retain the existing audit behavior.

### Claude Code

Export `MG400_GATEWAY_TOKEN` in the client environment, then use the checked-in configuration:

```sh
claude --strict-mcp-config --mcp-config src/gateway/clients/claude-code.mcp.json --tools ""
```

The configuration expands `${MG400_GATEWAY_TOKEN}` in the Authorization header. It stores no credential.
The command selects this MCP configuration and disables built-in tools. Do not add shell, device, or approval tools to this lab client.
Load the `operator_guidance` prompt through the client's MCP interface. Start with a request to list recipes or read service state.
Claude Code owns its transport and provider configuration. Keep proxy settings from routing the gateway credential outside the loopback connection.

### Local Qwen and smolagents

Use an existing local OpenAI-compatible Qwen server, or an authenticated tunnel to that server.
The example starts no model server and downloads no model weights. Set the exact served model ID in `QUMUS_DEFAULT_MODEL_ID`.
It uses smolagents `OpenAIServerModel` and `ToolCallingAgent`. The settings retain the `QUMUS_*` names from the earlier agent.

| Client setting | Purpose |
| --- | --- |
| `MG400_GATEWAY_URL` | HTTP loopback URL with an explicit port and `/mcp`; default `http://127.0.0.1:8773/mcp` |
| `MG400_GATEWAY_TOKEN` | Gateway bearer token; at least 24 printable ASCII characters |
| `QUMUS_DEFAULT_MODEL_ID` | Required model ID served by the local API |
| `QUMUS_API_BASE` | HTTP loopback URL with an explicit port and `/v1`; default `http://127.0.0.1:8000/v1` |
| `QUMUS_API_KEY` | Separate model credential; `unused` if the local server requires no credential |
| `QUMUS_LLM_TIMEOUT` | Model request timeout from 1 through 300 seconds; default 60 |
| `QUMUS_LLM_RETRIES` | Must be 0; automatic model request retries are disabled |

Export the gateway token separately. Edit the model values in `src/gateway/clients/qwen.env.example`, then run:

```sh
set -a
. src/gateway/clients/qwen.env.example
set +a
uv run --group gateway-clients python -m gateway.clients "List recipes. Do not propose a run."
```

The `gateway-clients` dependency group contains `smolagents[mcp,openai]`. It installs no GPU runtime or model weights.
The example reads the gateway's tool definitions through the MCP SDK and adapts them with `SmolAgentsAdapter`.
It requires exactly the 9 gateway tools. It adds no shell, code executor, direct device client, or approval tool.
Each MCP operation uses a fresh session with a 30-second deadline. Both HTTP clients ignore environment proxies and refuse redirects.
Unset `OPENAI_CUSTOM_HEADERS` before starting the example; it rejects inherited custom headers.
Explicit SDK settings override inherited API credentials, organization, project, and base URL.
The model client receives only its separate model credential. It receives no gateway credential in its instructions or tool definitions.

The example permits at most 1 proposal attempt per invocation, including failed or uncertain attempts. Its tool-calling loop permits at most 8 steps.
After a proposal, report its ID and pending decision. A person reviews and approves it on the dashboard.
A decision from `get_approval` gives the client no actuation authority. Use run records to report recorded outcomes.
If the reply is lost, do not rerun the proposal command. Have a person inspect proposals and current runs on the dashboard.

## Installation and demo

Add `gateway` to `MG400_OPTIONAL_SERVICES` and follow the [installation procedure](deploy.en.md#installation-and-updates).
The optional `mg400-gateway.service` uses the `gateway` dependency group and no device groups.
The installer prepares the private `runs` directory and generates a value for an empty gateway token setting. It does not start the service.
Configure the token and data paths before the owner runs `mg400ctl start gateway`.

For an offline demo, set a temporary token and run:

```sh
uv run --group gateway python -m gateway --demo
```

Demo mode uses a private empty run store, bundled recipes, and static service data.
It ignores production service URLs, downstream tokens, and data paths. It opens no device or downstream connection.
Demo mode disables proposal submission and lookup. The isolated orchestrator demo also discards the production gateway credential.
Offline approval tests use injected services and in-process HTTP transports. They open no instrument or listening port.
The demo audit disappears when the process exits. The gateway bearer token remains required.
