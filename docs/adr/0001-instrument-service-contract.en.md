[中文](0001-instrument-service-contract.md)

# 0001: Instrument service contract v1

## Status

Accepted: 2026-10-30.

## Context

Robot, stage, vision, and instrument services need the same discovery, control, and failure interfaces.

## Decision

Use contract version 1. Paths are relative to the configured service base path.

| Method and path | Request or response |
| --- | --- |
| `GET api/describe` | `{service, version, contract: 1, demo, zones, lease_seconds, on_lease_expiry, commands: {name: {params: <JSON Schema>, any_client: bool}}}` |
| `GET api/state` | `{service, seq, ts_unix, demo, connected, busy, locked, fault, lease, last_command, ...}` |
| `POST api/command` | Body `{"action": "...", ...}`. Return `202` with `{command_id, state}`. Completion appears in `state.last_command`. |
| `POST api/stop` | Body `{"wait_s": 0..15}`. Return `{stop, state}`, with `stop` equal to `confirmed`, `pending`, or `unconfirmed`. Any client may stop at any time. Repeated requests are safe. |
| `POST api/heartbeat` | Acquire or renew the caller's lease. Return `409` when another client holds it. |
| `POST api/release` | Release the lease only when the caller holds it. |
| `GET api/events?since=<seq>` | `{events: [{seq, ts_unix, level, message, fields}], next}`. These events also enter the service journal. |
| `GET api/ws` | Optional state push with robot protocol 1. |

- `fault` is `null` or `{code, message}`. `last_command` is `null` or `{id, action, client, result, message, finished_at}`. Results are `ok`, `failed`, `rejected`, or `stopped`.
- Include units in device fields: `pv_c`, `ramp_c_per_min`, `flow_pct`, `x_mm`, `r_deg`.
- POST requests require `Content-Type: application/json` and `X-MG400-Client`. A client ID contains 1 to 128 printable ASCII characters.
- Errors use `{"error": "..."}`: `400` invalid request, `401` token, `403` access rule, `409` state or lease conflict, `415` non-JSON, `503` lost device link.
- Bearer tokens contain at least 24 printable ASCII characters. Without a token, accept only loopback `Host` values and reject forwarded headers. Preserve Host, Origin, and forwarded-header checks.
- There is 1 controlling client. The default lease lasts 3 s. Reads and `stop` require no lease. Motion stops on expiry. The furnace follows [0005](0005-furnace-lifecycle.en.md). Declare the action in `on_lease_expiry`.
- An unknown command outcome sets `locked` and a `fault`. Refuse commands except `stop` until a person clears the lock. Never retry motion automatically.
- `--demo` substitutes simulated devices. Report it in `state.demo` and `describe.demo`. Clients reject a different `contract` version.
- Keep the robot's current protocol. Add `describe`, `stop`, `events`, and `last_command`.

## Consequences

Clients share control logic while services retain device-specific safety behavior. A confirmed stop and an unknown outcome remain distinct states.
