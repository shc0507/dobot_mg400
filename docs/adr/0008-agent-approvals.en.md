[中文](0008-agent-approvals.md)

# 0008: LLM agents behind approval gates

## Status

Accepted: 2026-10-30. Approval policy added: 2026-11-02 (H-23).

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
- Proposals follow the approval policy below.
- Store each approval's recipe, parameters, requester, reason, creation time, decision, and decider. Check policy limits before storing a proposal.
- The dashboard shows the full recipe and parameters, with Approve and Reject controls. An approved run starts through the orchestrator like a manual run.
- No gateway tool sends a device command. Rejection, expiry, and limit violations do not authorize execution. Approval does not remove service leases, stop controls, or safety checks.

### Approval policy (H-23, 2026-11-02)

The owner delegated this policy to the migration coordinator. The owner can change it in this ADR.

| Rule | Value |
| --- | --- |
| Approvers | A person at the dashboard. The gateway cannot approve, and an agent cannot approve its own proposal. Each approval records the approver's client identity. |
| Presence | Until H-24 approves unattended runs, approve a run only while a person in the lab can reach a stop control. |
| Allowed recipes | Recipes from the repository catalog whose steps use only `wait`, `sample.record`, `robot.enable`, `robot.stop`, `robot.pick`, `robot.place`, `robot.transfer`, `crucible.load` and `crucible.unload`. |
| Refused steps | `robot.move` with free coordinates, any furnace setpoint or sequence, any gas command, raw stage moves, microstep changes and firmware commands. |
| Temperatures | No heating from a proposal. The crucible limits (`load_max_c`, `unload_max_c`, `awake_s`, `max_age_s`) come from the host settings. A proposal cannot set them. |
| Speeds | No speed parameter above the recipe default. The service caps still apply. |
| Gases | None. |
| Expiry | 30 minutes without a decision. |
| Pending proposals | At most 3 at a time. |

## Consequences

Agents can inspect the lab without actuation authority. A proposal creates an auditable decision point before the orchestrator can start a run.
