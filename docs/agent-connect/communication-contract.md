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

`devices.stage.values.stage_reference` uses the same native cache snapshot as that frame. Its value is `null` when unavailable. Before a protected stage decision, compare this
value with `choices.stage_context.preconditions.stage_reference`. A newer choices response alone cannot establish which calibration reference produced a retained frame.

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
execution. Read authenticated `GET /commands/choices` for saved choices, `constraints.stage` and `stage_context`. These values can change without changing the definition digest.
Stage movement requires current calibration, limits, faults, lease and Stop-epoch checks. The server rechecks these conditions before movement. Do not reproduce the action catalog
or bypass it with direct service protocols.

`choices.stage_context` contains `version: 1`, boolean `available`, string `reason` and `preconditions`. When available, `preconditions` contains `stage_reference`, `profile_id`
and `profile_digest`. The context uses the same cached stage and selected-profile snapshots as the returned choices. When unavailable, `preconditions` is `null` and `reason`
explains why. Use the returned values exactly. Do not manufacture references or calculate profile digests in the model.

Protected stage actions have the reviewed `route` value `transfer` or `profile` in [ACTION_SPECS](../../src/orchestrator/agent_connect/actions.py).
This includes stage movement, saved targets and transfer-profile edits. Bind model decisions to this context for these actions. Apply this sequence:

1. Read choices and the state snapshot supplied to the model.
2. Check that choices provide an available, supported stage context.
3. Compare the frame's `stage_reference` with the choices reference. Reject a protected stage decision if either is missing or different.
4. Preserve that context before calling the model.
5. After the model selects a protected action, read fresh choices before `POST /commands`.
6. Reject that action if the context is missing, unavailable, malformed or changed.
7. Copy the original `preconditions` into the command envelope. Never replace them with the newer values.

The model selects the action and parameters. Connector code supplies the original preconditions. Server checks reject changes after the connector's final comparison and before
dispatch. This binding covers stage calibration references and the selected transfer profile. It does not freeze arbitrary sensor readings or check every position-derived
decision. Draft contents and other profiles named by action parameters are outside this binding. It adds no device process identity checks.

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
These preconditions apply only to stage actions with the reviewed `transfer` or `profile` route. Native actions such as `stage.calibrate` and `stage.clear_fault` use `{}`.
Other device actions also use `{}`. Stop keeps its independent path. For compatibility, the server accepts omitted preconditions or `{}` from older and manual clients.
The local model command path refuses protected stage actions without the captured context. Questions and other device actions do not require stage context. Read
[prepare()](../../src/orchestrator/agent_connect/action_backend.py) before implementing reference comparisons.

## Microscope calibration and autofocus

Read `devices.microscope.values.turret_profile`, `visual_focus`, `autofocus_run`, `autofocus_settings`, and `motor_power` in the state frame.
`turret_profile` reports the selected profile and whether its X alignment and initial 5x record are ready.
`visual_focus` reports the last marker observation. Only the 5x initial Y reference controls coarse positioning.
Camera coordinates use pixels normalized to a reference image width of `3840`. They are not physical distances.
Check measurement age and operation status. An idle saved measurement is not continuous tracking.
`motor_power` reports the objective and focus enable/sleep states when the firmware supplies them.
Unknown power fields must remain unknown on older firmware or a disconnected controller.
`focus_reference: "relative_command"` identifies a relative pulse coordinate for the current command.
`focus_position_pulses` and `focus_limits_pulses` do not identify absolute physical positions or end stops.
`focus_travel_limit_pulses` is the operator-set manual/coarse cumulative travel budget. Its default is `20000` quarter-step pulses.
`autofocus_settings` publishes `five_x_half_range_pulses`, `ten_x_half_range_pulses`, `twenty_x_half_range_pulses`, and `fifty_x_half_range_pulses`.
Their defaults are `5000`, `5000`, `5000`, and `3000`. Saved values override the defaults. Each operator-editable value is an integer from `4` to `1000000`.
Older saved files containing only the 5x setting retain that value and use defaults for the other lenses. There is no shared `other_half_range_pulses` field.
The half-range bounds each direction from the scan center. It is not the total pulse travel across scans and reversals.
Autofocus uses a cumulative budget derived from the selected range. The combined sequence uses 1 budget for all lenses and coarse positioning.
Travel in both directions counts toward the command budget. The service freezes settings when a command starts.
`visual_focus.travel_used_pulses` and `visual_focus.travel_limit_pulses` report the current or last command's reserved pulse travel and frozen budget. Reservation occurs before each move; after Stop or a failed acknowledgement, it can exceed the pulses actually sent. It is not a measured physical distance.
An agent with operator control can change these settings while the microscope is idle. Changes require the microscope lease and use the native settings endpoints. They do not start movement. Do not repeat or split movement commands to bypass an exhausted budget.

`autofocus_run` contains `phase`, `objective_id`, `stage`, `score`, `position_pulses`, `search_half_range_pulses`, `verified`, `completed`, `error`, and `sequence`.
`search_half_range_pulses` describes the active or last scan, using the settings frozen at dispatch. It is separate from accumulated movement and the cumulative budget.
Scan scores describe image sharpness; they are not a percentage or a measured sample distance.
Within the combined autofocus sequence, only a verified completion permits progression. A command acknowledgement, discovered peak, or idle motor does not establish successful focus.
Use the terminal command result with these fields. A retained result describes its recorded observation, not continuous sample tracking.

Use only these profile and focus actions from the current catalog:

| Action | Parameters | Result |
| --- | --- | --- |
| `turret.select` | `{"id":"<saved profile ID>"}` | Select a human-created profile. No movement. |
| `microscope.select_objective` | `{"id":1}` | Select any lens in any order. IDs are `0` = 5x, `1` = 10x, `2` = 20x, `3` = 50x. Align saved X at the current focus height. No focus movement or automatic autofocus. |
| `microscope.rough_focus_5x` | `{}` | Position the seated 5x lens approximately using its initial marker Y. No autofocus. |
| `microscope.focus_move` | `{"steps":10}` | Move by signed integer quarter-step pulses within the manual travel budget. Invalidate verified focus. |
| `microscope.autofocus` | `{"mode":"local"}` | Run autofocus using the current lens's saved range, matching the single Console Autofocus button. Agent Connect accepts only `local`. The native service retains `full` for older clients. |
| `microscope.autofocus_sequence` | `{}` | Require 5x initially. Perform coarse positioning, then autofocus 5x, 10x, 20x and 50x using each lens's saved range. Stop on failure. |
| `microscope.set_autofocus_settings` | `{"fifty_x_half_range_pulses":3000}` | Save a nonempty subset of the 4 range fields. Each value is an integer from `4` to `1000000`. Omitted ranges remain unchanged. No movement. |
| `microscope.set_focus_travel_limit` | `{"pulses":20000}` | Save the manual/rough cumulative travel limit. Accept an integer from `1` to `2147483647`. No movement. |

`microscope.autofocus` uses revision `8`. `microscope.select_objective` and `microscope.autofocus_sequence` use revision `7`. The 2 settings actions use revision `3`. The other listed actions use revision `6`.
Read `choices.actions["turret.select"].id` for saved IDs and labels.
Use the normal command envelope, session, operator grant, and result polling.
Profile selection and connection do not start motion. Objective selection does not start autofocus.
The service verifies marker identity before motion and may reject an unreadable or inconsistent view.
Separate lens selection requires neither prior autofocus nor a fresh sharpness check.
It does not restore per-lens Y positions. The operator is responsible for physical clearance and selection order.
The optional combined sequence requires 5x initially and advances only after verified autofocus.
Profile, identity, busy-state, Stop, and lease checks remain active.
After a sleep or release, do not assume that the previous pulse count still represents the mechanism's position.

Turret calibration creation, recording, editing, upload, deletion, and draft loading are human operations.
Their former Agent Connect actions are absent. A request for one is rejected before device control or dispatch.
`microscope.set_focus_zero`, `microscope.home_focus`, `microscope.focus_to_clearance`, `microscope.focus_to_saved_height`, and simulated `camera.turret_frame` actions are absent.
An agent must not use a raw Console route, device serial command, or another interface to bypass this boundary.
Incomplete and older profiles require the new human-recorded 5x initial reference before operation. Saved files are not silently rewritten.
Keep calibration instructions in the project knowledge supplied to the runtime model.

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
mode flags rather than assuming deployment from the URL. Reviewed actions preserve device leases, native speed and alarm checks, stage travel limits, microscope focus travel budgets and switching-height checks, and temperature and ramp limits.
Further device-service process-ID integration and deployment remain deferred. Console boot identity, client identity and control epoch are active now. Do not invent missing service
identities, source times, capture times or reference continuity.

Test the external adapter offline for these cases:

- Valid and damaged digests, unsupported dialects, changed units, optional fields and different identity namespaces.
- Missing, null, stale, uncalibrated and unavailable measurements, incomplete cached fields and conflicting chip evidence.
- Sample pagination revisions, exact chip IDs, slow readers, replay gaps, contract changes and Console restarts.
- Invitation expiry, observer-only connection, explicit grant, reconnect, token expiry and heartbeat loss.
- Catalog schema checks, runtime choices, missing or malformed stage context, stale references, changed epochs and server-side physical-mode restrictions.
- Stage context changes during state capture, model interpretation or submission, with original preconditions preserved and unrelated actions available.
- Lost command responses, duplicate IDs, changed bodies, asynchronous rejection, cancellation and unknown outcomes.
- Stop without authority, repeated Stop delivery, fallback without credentials and unconfirmed device outcomes.
- Furnace command completion versus sequence completion, with no new shutdown policy after control loss.

Use [agent-connect.en.md](../agent-connect.en.md) for the operator workflow. Use [routes.py](../../src/orchestrator/agent_connect/routes.py),
[control.py](../../src/orchestrator/agent_connect/control.py) and [command_routes.py](../../src/orchestrator/agent_connect/command_routes.py) for route definitions. Use
[actions.py](../../src/orchestrator/agent_connect/actions.py) and the current catalog for exact action schemas. The native instrument contract in [ADR
0001](../adr/0001-instrument-service-contract.en.md) is a separate interface below the Console. Do not replace Agent Connect with direct service requests or a CLI fallback. Do not
use the separate `/lab/api/agent-fixtures` format as the production read protocol.
