[中文](0010-agent-cli.md)

# 0010: Shell agents operate devices through labctl

## Status

Accepted: 2026-11-09. Supersedes [0008](0008-agent-approvals.en.md).

## Context

The lab is a test stage. The owner needs agents to run and test every device. ADR 0008 keeps device actuation by LLM agents behind an explicit human decision. Agents that run in a shell on Olares need direct device control.

## Decision

- Shell agents on Olares may operate devices with the `labctl` device groups. See the [agent CLI](../agent-cli.en.md).
- The Lab Console holds the agent leases. An agent session is a console page `agent-<session>`, with a 10 minute idle timeout.
- The services keep the AGENTS.md test-stage protections. The CLI adds no checks, settings or tokens.
- There is no MCP gateway and no proposal flow.

## Consequences

The Overview **Control** card shows each agent holder. The owner takes control back with **Release all**. Stops never need control.
