[中文](0008-agent-approvals.md)

# 0008: LLM agents behind approval gates

## Status

Accepted: 2026-10-30.

## Context

Agents need lab state and run history. Device actuation must remain subject to an explicit human decision.

## Decision

Introduce the LLM gateway in phase 5. Use the Python MCP SDK with Streamable HTTP, loopback port 8773, and a bearer token.

| Tool | Permitted action |
| --- | --- |
| `list_services`, `get_state(service)` | Read service discovery and state |
| `list_recipes`, `describe_recipe` | Read available recipes and their definitions |
| `list_runs`, `get_run`, `get_chip(id)` | Read run history and chip records |
| `propose_run(recipe, params, reason)` | Create an approval request after policy checks |
| `get_approval(id)` | Read an approval decision |

- Initially expose only the read tools. Reads require no actuation approval. Record every gateway call in the run store with caller, tool, arguments, and time.
- Before enabling proposals, the owner defines permitted recipes, approvers, and temperature, speed, and gas limits. Those policy values remain unresolved until the owner supplies them.
- Store each approval's recipe, parameters, requester, reason, creation time, decision, and decider. Check policy limits before storing a proposal.
- The dashboard shows the full recipe and parameters, with Approve and Reject controls. An approved run starts through the orchestrator like a manual run.
- No gateway tool sends a device command. Rejection, expiry, and limit violations do not authorize execution. Approval does not remove service leases, stop controls, or safety checks.

## Consequences

Agents can inspect the lab without actuation authority. A proposal creates an auditable decision point before the orchestrator can start a run.
