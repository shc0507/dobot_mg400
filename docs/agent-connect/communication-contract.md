# Agent Connect communication contract

Read [project-knowledge.md](project-knowledge.md) before this contract. This document specifies the Console interface for external coding agents. It does not authorize equipment
operation.

## Integration before execution

1. Wait for the human to request integration or adaptation of the external agent.
2. Read both documents before changing the external adapter.
3. Implement and test the adapter offline against the referenced schemas and simulated responses.
4. Present unsupported interfaces and required code changes to the human.
5. Start equipment work only after the human requests that work and grants operator control.

Runtime checks detect changes to definitions, identity, units, references and authority. They do not authorize code changes, new mappings or automatic recovery actions. The adapter
can refresh compatible fields and choices within its implemented, tested support. If a change exceeds the adapter's tested support, suspend dependent decoding and normal command
submission. Report the changed fields and return to human-directed adaptation. Keep raw responses available for diagnosis. Keep the independent Stop path available.

## Origins, routes and authentication

The development target is `http://100.64.0.1:8082` through LarePass VPN. The human Console is `/lab/#agent-connect` on that origin. Operator control applies only to this Console.
It does not provide authority over 8080 or other direct device interfaces. The local portal is `http://127.0.0.1:8792/`. The simulated Console uses `http://127.0.0.1:8790/lab/` and
a separate WebSocket listener on port `8793`. Keep live and simulated origins, identities, credentials, results and cursors separate. Never replace unavailable live data with
simulation data.

The read base is `/lab/api/agent`. The control base is `/lab/api/agent/control`. Use JSON request bodies with `Content-Type: application/json`. Control POST routes reject a
supplied `Origin` that differs from the Console origin. Read discovery, state, samples, stream and command catalog require no agent session. For agent status, heartbeat, choices,
command submission and result reads, send `Authorization: Bearer <session_token>`. Never omit the token to gain trusted Console management privileges. Do not send the pairing
credential as a session token.

| Method | Path relative to control base | Purpose |
| --- | --- | --- |
| GET | `/contract` | Read control version, policy, paths and heartbeat settings. |
| GET | `/status` | Read command identity, operator, epoch, session and enabled modes. |
| POST | `/pair` | Redeem an invitation for a client credential. |
| POST | `/pair/status` | Read access status with `{"credential":"<credential>"}`. |
| POST | `/connect` | Exchange the credential for an observer session. |
| POST | `/heartbeat` | Renew the session with `{}` and its Bearer token. |
| POST | `/disconnect` | End the session with `{}` and its Bearer token. |
| POST | `/release` | Release operator control with `{"control_epoch":"<current epoch>"}`. |

The human creates an invitation in the Console. An invitation has 8 readable characters, usually shown as `XXXX-XXXX`. It expires after `300` seconds and works once. The name must
contain `1` to `64` printable characters after trimming. These examples contain placeholders only:

```http
POST /lab/api/agent/control/pair
Content-Type: application/json

{"invitation":"<invitation>","name":"External agent"}
```

Pairing returns `client_id`, `credential`, `matching_code` and `status: "approved"`. It immediately permits connection, without a second approval step.

```http
POST /lab/api/agent/control/connect
Content-Type: application/json

{"credential":"<credential from pair>"}
```

Connect returns `session_token`, `session_id` and `state`. This initial `state` is the authority snapshot, not the complete `/status` response. Read `/status` after connection for
command fields and current mode. Connecting again replaces that client's previous session. Every new session starts as an observer. The human selects **Grant control** for the
connected agent in the Console. The external agent must not call human invitation, grant, takeover, revoke or warning-management actions. Only `1` operator can control this Console
at a time. A connection or grant acquires no device lease and sends no equipment command.

Each `clients` entry in an authority snapshot or `/status` includes the additive field `last_connected_at`: Unix seconds (`number`) or `null`.
It records the most recent successful connection or reconnection and survives Console restarts. New clients and historical records without a recorded connection time return `null`.
Pairing, heartbeats, status reads, control release and rejected connections do not update it. The field does not indicate current connectivity or operator control.
Older Console versions may omit it.

## Definitions and identity

Read `GET /lab/api/agent/discovery` before decoding state. Discovery version `1` provides `mode`, `boot_id`, `contract_version`, `contract_id`, `contract_url`, `endpoints`,
`release` and control metadata. Follow the advertised endpoint paths on the selected Console origin. The current read bundle is `agent-connect`, version `1.1.0`, with JSON Schema
`2020-12`. Its bundle metadata reports Phase `7`. The control contract reports version `6` and Phase `8`. The command catalog reports version `1`. These are different version
namespaces. Do not use a phase number as the compatibility test.

Download the read bundle from `/lab/api/agent/contracts/<hex digest>.json`. The URL excludes the `sha256:` prefix in `contract_id`. The immutable response contains schemas and
semantics, not live readings. Check its digest before caching it or decoding state. The exact read encoding is
[canonical_bytes()](../../src/orchestrator/agent_connect/contract.py):

```python
encoded = json.dumps(bundle, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
contract_id = "sha256:" + hashlib.sha256(encoded).hexdigest()
```

The accepted definition subset uses ASCII object keys, strings, booleans, null, arrays and integers within `±(2**53 - 1)`. It excludes floating-point values in the bundle. Do not
claim general JSON Canonicalization Scheme support. Cache verified bundles by origin and digest. Check the dialect, required fields, types, units and reference semantics that the
adapter uses. An optional field is acceptable only if the tested adapter can ignore it without changing meaning. An unsupported dialect, changed unit or damaged digest requires
human-directed adaptation.

| Identity | Meaning and required use |
| --- | --- |
| Read `boot_id` | Read producer identity. Pair it with `event_seq` for replay. |
| Read `contract_id` | Digest of the read bundle. Decode each frame or event with its own digest. |
| Status `command_boot_id` | Command-control identity. Copy it into command `boot_id`. |
| Status `command_contract_id` | Must equal the selected catalog's `contract_id`. |
| `control_epoch` | Exact returned string for current ownership. Do not parse or manufacture it. |
| `client_id` | Paired client identity. Command request IDs belong to this client. |
| `session_id` | Current connection identity. A replacement session needs a new grant. |
| Device `instance` | Optional source-process identity. Missing identity means unknown continuity. |
| Reference revision | Coordinate-reference identity. A changed revision invalidates dependent assumptions. |

Discovery sets `identity_scope: "read_stream"`. Control status sets `identity_scope: "command_control"`. Never substitute the read boot ID or digest in a command envelope. Restart
invalidates sessions and grants, even when the release remains unchanged.

Discovery `release` contains `release_id`, `source_bundle_id`, both contract IDs, `source`, `complete` and `scope: "this-console"`. The release digest combines the startup source
digest with both current contract digests. The source digest covers bounded orchestrator Python, JavaScript, CSS and HTML files. It does not identify all shared services or
firmware. `source` includes `digest`, `complete`, `file_count`, `bytes_hashed` and `scope`. Incomplete metadata remains incomplete. The running process retains its startup source
identity after files change on disk. Compare the release ID and both contract IDs against the reviewed adapter support. See
[release.py](../../src/orchestrator/agent_connect/release.py) for the exact digest construction and bounds.

## State, measurements and chip evidence

`GET /lab/api/agent/v1/state` returns a full selected snapshot. The envelope contains `type: "state"`, `boot_id`, `contract_id`, `seq`, `generated_at` and `event_seq`. Selected
groups appear as `devices`, `samples` and `operations`. `mode` distinguishes live data from `MOCK` data. `seq` identifies assembled frames. It is not the event replay cursor. Use
[protocol.py](../../src/orchestrator/agent_connect/protocol.py) for exact required and optional fields. The `operations` schema remains a generic object. Do not infer a complete
command record from it.

Each device requires `quality`, `sampled_at`, `age_s`, `instance` and `values`. Quality is `valid`, `stale`, `unavailable`, `uncalibrated` or `unknown`. Unknown quality values must
not become usable measurements. Read nested axis and observation quality independently from device quality. Missing fields, nulls and unavailable readings are not zeros or cleared
faults.

| Field | Meaning |
| --- | --- |
| `generated_at` | Frame assembly time in Unix seconds. |
| `sampled_at` | Source sample time in Unix seconds, when available. It need not be sensor capture time. |
| `state_at` | Service state assembly time in Unix seconds. |
| `observed_at` | Console cache update time in Unix seconds. |
| `age_s` | Time since the Console observed source state. It is not sensor age. |
| `frame_age_s`, `telemetry_age_s`, `encoder_age_s` | Source age plus cache age, where available. Null means unknown. |

Stage `sampled_at` remains null because its native assembly time is monotonic, not Unix capture time. A current frame does not make an old measurement current. Stage positions use
rotations. Robot pose uses `mm,mm,mm,degree`. Robot joints use degrees. Furnace temperatures use degrees Celsius. Command schemas specify ramps and gas flow units. Do not infer
conversions from numeric values.

`cached_state` adds bounded native `values`, a `units` map, `complete`, `omitted` and `omitted_count`. Unit keys are JSON Pointer paths within `cached_state.values`. The copy
excludes private fields, paths, images, thermal matrices and other oversized or unsupported values. Check omissions before treating this copy as complete. `data_gaps` reports
absent source metadata, including source identity and timestamps. Thermal observations contain shared statistics. Vision observations contain existing results without new capture
jobs. Historical vision results have `unknown` quality for the present scene. An absent capture timestamp remains null.

Chip IDs are exact strings. The current chip schema uses `chip_id`, `location`, `basis` and `revision`. `location.holder` is `unknown`, `tray_slot`, `robot` or `stage_insert`.
Location can include `tray_id`, `slot_id`, `container_id` and nullable `inside_furnace`. Evidence basis is `observed`, `reported`, `inferred` or `unknown`. Other fields include
`updated_at`, `last_observation`, `last_known_location`, `last_run`, `last_step`, `pending_operation`, `conflict` and `passport_url`. Record update time differs from source
observation time. Accepted motion does not prove chip arrival. Historical work does not establish current location. A handoff can make the holder unknown while retaining the last
known location and pending destination. Conflicts make the disputed claim unknown. Old frames cannot restore an outdated location. The holder and furnace entry are separate claims.
`inside_furnace` defaults to null. A known entry claim requires valid mapping, reference, alignment and an established stage holder.
Live manual location reports remain disabled.

## Selection, pagination and replay

State and stream query options are `groups`, `rate_hz`, `selection` and repeated `chip_id`. `groups` is a comma-separated subset of `devices,samples,operations`. Rates are `1`, `2`
or `4` Hz, with default `2`. Selection is `active` or `ids`, with default `active`. Active chips have a pending operation or started work in a running or paused run. Explicit
selection requires `1` to `50` chip IDs, each containing `1` to `256` characters. Use repeated `chip_id` fields for IDs containing commas. The alternate `chip_ids` option splits at
commas.

`GET /lab/api/agent/v1/samples` also supports `selection=all`, `limit`, `offset` and `expected_revision`. Page size is `1` to `100`. Offset is nonnegative. Preserve returned
`revision` as `expected_revision` on later pages. If the revision changes, restart pagination. Read `items`, `count`, `total`, `complete`, `selection`, `next_offset` and
`revision`. `complete` means no later page remains for this selection. Earlier pages still belong to that selection.

Use the advertised `/v1/stream` WebSocket for receive-only messages. On the live origin, use `ws://100.64.0.1:8082/lab/api/agent/v1/stream`.
For the simulated Console, use `ws://127.0.0.1:8793/lab/api/agent/v1/stream`. The socket rejects application
messages, including commands, with close code `1008`. The shared producer assembles frames at `4` Hz. Device measurements can update more slowly. Subscribers reuse cached readings.
A slow subscriber retains the newest unsent state, not every intermediate frame. Read discovery `limits` for bounded subscriber counts, frame sizes and supported rates.

An event contains `type: "event"`, `boot_id`, `contract_id`, `event_seq`, `kind` and `data`. Process events in `event_seq` order within their read `boot_id`. Reconnect with both
`boot_id` and `event_seq` query fields. The server sends a current snapshot before retained events. Do not advance the saved event cursor merely because that snapshot contains a
newer `event_seq`. Advance it as the adapter processes events, or establish a fresh baseline after resynchronization. Events keep their original contract digest. Replayed events do
not redefine the current agreement. A `resync` message reports `boot_changed`, `cursor_ahead` or `history_evicted`. Discard continuity assumptions after a gap and consume a fresh
snapshot. On `contract.changed` or an unfamiliar frame digest, repeat discovery and definition checks. An evicted contract returns HTTP `404`. Do not decode its events with the
latest schema.

## Catalog, runtime choices and command envelope

Read `GET /commands/catalog` under the control base before selecting an action. The catalog contains `version`, `contract_id` and `actions`. Descriptors include `id`, `label`,
`description`, `params`, `defaults`, `device`, `available`, `reason`, `revision` and mode metadata. Use the exact action ID and its `params` schema. Defaults do not authorize
execution. Read authenticated `GET /commands/choices` for saved choices and `constraints.stage`. Choices and cached stage limits can change without changing the definition digest.
Stage movement requires current calibration, limits, faults, lease and Stop-epoch checks. The server rechecks these conditions before movement. Do not reproduce the action catalog
or bypass it with direct service protocols.

The command digest hashes `{"version":1,"actions":[...]}` after sorting actions by `id`. Remove only `available` and `reason` from each action before hashing. Use sorted object
keys, compact separators, UTF-8 and Python JSON's default ASCII escaping. Reject nonfinite numbers. Preserve the server's numeric encoding. This differs from the read-bundle
encoding. See [catalog()](../../src/orchestrator/agent_connect/commands.py) and [canonical()](../../src/orchestrator/agent_connect/command_store.py) for exact behavior. Changing
availability alone does not change the digest.

Submit `POST /commands` with the session Bearer token. The following template is not an executable command:

```json
{
  "request_id": "external-request-1",
  "boot_id": "<status.command_boot_id>",
  "contract_id": "<catalog.contract_id>",
  "control_epoch": "<status.control_epoch>",
  "action": "<human-selected catalog action>",
  "params": {},
  "preconditions": {}
}
```

Replace `params` with the selected action's checked parameters. Send only these fields. `preconditions` is optional. Both objects must contain finite JSON values. Request IDs match
`[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`. The other identity strings and action are nonempty and at most `256` characters. The body limit is `6 * 1024 * 1024 + 1024` bytes. Supported
stage preconditions are `stage_reference`, `profile_id` and `profile_digest`. `stage_reference` accepts `reference_revision`, `stop_epoch`, `limits_digest` and `reference_source`.
These preconditions apply only to supported stage motion or profile actions. Use `{}` elsewhere. Read [prepare()](../../src/orchestrator/agent_connect/action_backend.py) before
implementing reference comparisons.

## Results, errors and cancellation

Submission returns HTTP `202` and an operation record. It does not prove dispatch or completion. Validation can reject the operation after admission. Poll `GET
/commands/request/<request_id>` with the session token until the adapter receives a terminal result. `GET /commands/history` returns up to `50` summaries and total `count`.
Summaries omit parameters and full results. Read the full record for those fields. Agents can access only their client's records.

Records include identity, action, parameters, preconditions, `status`, timestamps, `dispatch_count`, `progress`, `result`, `error` and cancellation information. Active statuses are
`accepted`, `running` and `cancelling`. Terminal statuses are `succeeded`, `failed`, `rejected`, `stopped` and `unknown`. The Console admits only `1` active normal operation at a
time. A completed furnace command does not mean the thermal sequence finished. Read native sequence state and measurement quality separately.

An identical request ID and canonical body return the saved record without another dispatch. A different body under that client's existing ID returns HTTP `409`. Omitting
`preconditions` and sending `{}` produce different submitted bodies. Preserve the exact body for recovery. Duplicates and result reads require a current session. After a lost
response, query the original request ID. Do not automatically submit movement with a new ID. Restart changes unfinished stored operations to `unknown` and never replays them.

Service errors return an HTTP status and JSON `error` message. Handle `400` for invalid input, `401` for invalid access, `403` for denied authority or mode, and `404` for unknown
actions or records. Handle `409` for stale identity, changed contracts, unavailable actions, busy operations or conflicting request IDs. Handle `413` for oversized input, `415` for
incorrect content type and `503` for unavailable services or storage. An HTTP failure and an operation's `error` field are separate result channels. Do not interpret timeout,
disconnection or HTTP `202` as success. Identity or contract errors require fresh status and discovery checks before any new human-requested command.

Cancel with `POST /commands/cancel` and `{"request_id":"<owned request ID>"}`. Cancellation requests termination of agent work. It does not prove equipment stopped. Furnace
cancellation preserves native run and recovery behavior. Use the explicit furnace or gas Stop target when those outputs must stop.

## Independent Stop and connection loss

Send `POST /lab/api/agent/control/stop` with `{"request_id":"external-stop-1","target":"all_motion"}`. Stop requires no session, grant, current epoch or contract digest. Keep Stop
callable when normal command admission, schema validation or status refresh fails. Allowed targets are `stage`, `robot`, `microscope`, `all_motion`, `furnace`, `gas_all`, `gas_0`,
`gas_1` and `gas_2`. `all_motion` excludes the furnace. `furnace` requests native heat and gas Stop. The server determines sender attribution. Do not add sender fields to the body.
Unknown credentials do not block Stop. Before authority initialization, normal Stop records an unverified sender. With initialized authority, a request without an Authorization
header can receive trusted-human attribution. Invalid or unavailable identity remains unverified. The independent gateway always records an unverified sender.

Each deliberate Stop uses a new request ID. Repeated deliveries with the same ID and target share a history record, but each delivery still dispatches Stop. This differs from
normal command idempotency. Read `/stops` and `/stops/<request_id>` under the control base for delivery and device outcomes. Statuses are `pending`, `confirmed`, `unconfirmed`,
`unknown` or `disabled`. Receipt does not prove that equipment stopped. An unknown delivery remains unknown after another delivery succeeds.

On a network failure or HTTP `503` from normal Stop, use the fixed independent gateway. The live base is `http://100.64.0.1:8082/lab/api/agent/emergency`. The simulation base is
`http://127.0.0.1:8794`. Both expose `/stop`, `/status`, `/stops` and `/stops/<request_id>`. Preserve the Stop request ID and target. Send no credentials to the fallback. Check
gateway `equipment_mode` and `stop_enabled` without delaying an urgent Stop. The gateway exposes fixed Stop and status handlers only. It cannot execute normal commands. It is a
separate process, but still depends on network, proxy and device services. Retain both histories after recovery. Never replay saved Stops automatically.

Send a session heartbeat every `2` seconds independently of browser visibility and command polling. The session expires after `10` seconds without heartbeat. Loss of the
controlling session revokes its grant, cancels further agent work and releases its device leases. The Console records a sender warning with `policy: "warn_only"` and
`shutdown_enabled: false`. Reconnection requires another explicit human grant. Control loss adds no automatic furnace shutdown or new cooling or gas policy. Preserve native furnace
sequence, recovery and orphan behavior. Report disconnected gas controllers and unverified readings as such.

## Supported boundary and adapter checks

The Console constructor controls physical dispatch. Requests cannot enable it. The library defaults to disabled physical dispatch. The 8082 physical Console advertises enabled
commands, physical commands, furnace control and Stop. The simulator advertises simulated commands and Stop, with physical commands and furnace control disabled. Check the current
mode flags rather than assuming deployment from the URL. Reviewed actions preserve device leases and native speed, alarm, travel, focus, clearance, temperature and ramp limits.
Further device-service process-ID integration and deployment remain deferred. Console boot identity, client identity and control epoch are active now. Do not invent missing service
identities, source times, capture times or reference continuity.

Test the external adapter offline for these cases:

- Valid and damaged digests, unsupported dialects, changed units, optional fields and different identity namespaces.
- Missing, null, stale, uncalibrated and unavailable measurements, incomplete cached fields and conflicting chip evidence.
- Sample pagination revisions, exact chip IDs, slow readers, replay gaps, contract changes and Console restarts.
- Invitation expiry, observer-only connection, explicit grant, reconnect, token expiry and heartbeat loss.
- Catalog schema checks, runtime choices, stale references, changed epochs and server-side physical-mode restrictions.
- Lost command responses, duplicate IDs, changed bodies, asynchronous rejection, cancellation and unknown outcomes.
- Stop without authority, repeated Stop delivery, fallback without credentials and unconfirmed device outcomes.
- Furnace command completion versus sequence completion, with no new shutdown policy after control loss.

Use [agent-connect.en.md](../agent-connect.en.md) for the operator workflow. Use [routes.py](../../src/orchestrator/agent_connect/routes.py),
[control.py](../../src/orchestrator/agent_connect/control.py) and [command_routes.py](../../src/orchestrator/agent_connect/command_routes.py) for route definitions. Use
[actions.py](../../src/orchestrator/agent_connect/actions.py) and the current catalog for exact action schemas. The native instrument contract in [ADR
0001](../adr/0001-instrument-service-contract.en.md) is a separate interface below the Console. Do not replace Agent Connect with direct service requests or a CLI fallback. Do not
use the separate `/lab/api/agent-fixtures` format as the production read protocol.
