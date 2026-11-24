# Agent Connect

Agent Connect publishes cached device data and provides catalog commands after an explicit operator grant. The control interface uses version 6 and Phase 8. The 8082 production Console enables physical commands, including the supported furnace commands. Read streaming, normal commands and emergency Stop use separate paths.

To integrate a different agent, use the [external agent integration pack](agent-connect/README.md). A human starts adaptation with the prompt in the [root README](../README.en.md#connect-an-external-agent). Read project knowledge first, then the communication contract. Adapt and check the external connector before operation. Runtime discovery checks compatibility and supplies current values; it does not trigger autonomous code changes. This page documents operation of the existing portal and Console.

## Local portal

Enable the VPN in LarePass. Run the portal from the repository root:

```powershell
uv run python scripts/agent_portal.py
```

The command opens Agent Portal at `http://127.0.0.1:8792/`. The Live equipment source connects to `http://100.64.0.1:8082`. If the VPN or source is unavailable, the portal shows an error. It does not substitute simulated data.

The **Agent** tab accepts questions and instructions. Open **Model APIs** to configure a profile:

1. Select **Direct provider** or **Token Station** in **API route**.
2. Enter a profile name, model ID and API key.
3. For **Direct provider**, select the provider.
4. Leave **Base URL** empty for the default address, or enter a custom base URL.
5. Select **Save profile**.

Profiles save `route` as `direct` or `token_station`. Profiles without `route` use `direct`. **Direct provider** uses the selected provider's native API:

| Provider | API and output |
| --- | --- |
| OpenAI | Responses with structured output. |
| Anthropic | Messages with structured output. |
| Gemini | GenerateContent with a JSON schema. |

**Token Station** hides the provider selector and uses the Chat Completions protocol. Keep vendor prefixes in model IDs, for example `openai/gpt-6.1-sol`. Its default base URL is `https://physics.bytefuture.ai/v1`. The default request URL is `https://physics.bytefuture.ai/v1/chat/completions`. Requests use `POST`, `Authorization: Bearer <api_key>`, `model`, `messages` and `stream: true`.

For Token Station, the agent collects the complete server-sent events (SSE) response. It validates the decision JSON before submitting any command. Partial tokens never trigger commands. If the stream is truncated or reports an error, the agent submits no command.

Select **Activate** to use a saved profile. Saving alone does not activate it. **Deactivate** stops model use and retains the profile. Select **Delete** to delete a profile. Deleting the active profile leaves no active model. The 1 active profile applies to both live and simulated sources.

Windows DPAPI encrypts the complete saved payload for the current Windows user, including API keys and profile metadata. The default file is `%LOCALAPPDATA%/AI Chemistry Console/agent-portal/model-profiles.json`, outside Git. Restarting the portal restores saved profiles and the active selection. Without Windows credential protection, profile storage is unavailable. There is no plaintext fallback. The default profile mode does not load `OPENAI_API_KEY` automatically.

Profile changes and portal restarts send no automatic API probe or equipment command. When you submit a request, instructions and recent conversation go to the active API endpoint. The request also includes reviewed project knowledge, verified Console state and the current command catalog. Requests exclude camera video and Console credentials.

At runtime, the local agent loads reviewed project knowledge and checks the current read contract before using state. It compares supported actions with their reviewed schemas, revisions, defaults and devices. A mismatch requires a human-directed coding-agent update before normal commands can resume. The local agent does not change its code autonomously.

After connection and an explicit operator grant, clear instructions execute immediately. Missing targets or units cause a clarification question. Each instruction can execute 1 catalog action, including an existing composite action. Stage positions use rotations. Robot X/Y/Z use millimetres; R uses degrees. The agent reports the Console result and retains the request ID when the outcome is unknown. It never resends movement automatically. **Cancel agent task** cancels pending interpretation and requests cancellation of its own command. Stop controls remain available across the portal tabs.

The live activity list updates every 1 second. It shows state reads, interpretation, validation, command submission and Console results. Each submitted command includes its exact JSON body and request ID. The interpretation gives a brief action explanation; the list does not expose private model reasoning. Activity stays in process memory and is separate for live and simulated sources.

The **Digital Twin** tab shows the live Console state. **Manual testing** contains command forms, target previews and optional simulations. Opening a different tab does not disconnect the agent. Only an explicit instruction or manual command can request equipment operation. Opening the page, configuring the model and granting control do not start equipment.

The local server relays the read-only Olares WebSocket as server-sent events (SSE). It subscribes to `devices`, `samples` and `operations`. Updates per second selects 1, 2 or 4 Hz. The default is 2 Hz. The reader's Disconnect button stops only the data connection.

Digital Twin displays every device received from the Console stream. It shows stage position diagrams, robot coordinates, furnace temperatures, gas readings, thermal camera readings, microscope state and vision results. Missing readings remain unknown. Device inspectors and Full state JSON retain all received fields. Active samples, Operations and the last 100 events remain available below the device cards.

Stage diagrams use the lower and upper calibration limits from each stream frame, in rotations. Each new frame updates the displayed limits. Unset native limits remain unavailable. The portal never replaces them with the protocol's numeric range. To recalibrate, use the existing controls in the [Stage panel](stage-panel.en.md).

The browser checks the immutable data agreement and its digest before decoding. Unsupported definitions or a digest mismatch stop decoding. Raw JSON remains available.

In Manual testing, Send command groups actions by device. The form shows the parameters, a target preview where supported, and the outgoing JSON. A target preview does not move equipment. Only Send command submits a manual request. The visualizer uses the existing machine interfaces and adds no camera video to the data stream.

Use `--no-browser` to start without opening a tab. Press Ctrl+C to stop the servers. These processes bind only to loopback:

| Process | Ports | Purpose |
| --- | --- | --- |
| Portal | 8792 | Show live data and relay explicit commands. |
| Mock Console | 8790, 8793 | Serve simulated HTTP and WebSocket interfaces. |
| Independent simulator and Stop gateway | 8794 | Keep simulated stage Stop and status available when the mock Console stops. |

Close another Console preview before starting the portal. Both use port 8790. The appearance control selects dark or light mode and saves the preference locally.

## Agent connection and control

Operator ownership applies only to the 8082 test Console. It does not lock 8080 or other direct device interfaces. Mark coordinates testing so nobody operates equipment through those interfaces during agent control. Existing per-device leases remain active.

Public read streaming requires no invitation or operator grant. Console management trusts access through the lab network. It requires no owner sign-in, password, owner code or SSH tunnel. Human controls retain their device leases. When an agent holds operator control, select Take control before normal human writes.

### Console policy

Open `http://100.64.0.1:8082/lab/#agent-connect` through LarePass. The control contract is at `/lab/api/agent/control/contract`. It declares `pairing: {approval: "invitation", operator_grant: "explicit"}`. Status reports `management_available`.

The **Connections** subtab opens by default. Use it for invitations, agent records and operator control. **Testing** contains simulation controls and data and service details. Device Stop remains available outside both subtabs. Switching subtabs does not start equipment.

Library construction defaults to `allow_physical=False`. Production registration opts in with `allow_physical=True`. This server-side choice cannot be changed by an invitation, grant, command parameter or portal control.

| Source | Mode and command policy |
| --- | --- |
| 8082 production Console | `equipment_mode: "physical"`, `commands_enabled: true`, `physical_commands_enabled: true`, `furnace_control_enabled: true`, `stop_enabled: true`. |
| Local simulation | `equipment_mode: "simulated"`, `commands_enabled: true`, `physical_commands_enabled: false`, `furnace_control_enabled: false`, `stop_enabled: true`. |
| Library without physical opt-in or simulation | `equipment_mode: "disabled"`, `commands_enabled: false`, `physical_commands_enabled: false`, `furnace_control_enabled: false`, `stop_enabled: false`. |

Read the catalog before choosing an action. Supported actions use reviewed native handlers for the stage, robot, microscope, thermal camera, vision and furnace. An unsupported service capability remains unavailable with a reason. The interface provides no arbitrary device command, URL, recipe or composite dispatch.

Commands preserve native limits and device leases. These include robot speed and alarm checks, configured stage limits, microscope focus and turret clearance limits, and furnace temperature and ramp caps.

### Connect and send from a PC

Use Agent connection and control in the portal. Its control connection and read stream use separate requests to the same 8082 Console.

1. In the Console, select Create invitation. The single-use invitation expires after 300 seconds. Select Copy.
2. In the portal, enter an Agent name and the invitation. Select Use invitation.
3. Select Connect. The agent connects as an observer.
4. Release existing device control. Wait for runs, jobs and Sample Transfer to finish.
5. In the Console, select Grant control for the connected agent.
6. In the portal, choose an Action. Enter its parameters. Select Send command once.

The invitation contains 8 readable characters, shown as `XXXX-XXXX`. Copy works on the HTTP Console page. A valid invitation grants connection access immediately. The Console requires no separate approval.

Only 1 operator can hold this Console at a time. Grant control acquires no device lease and sends no command. The user chooses the first physical command and its parameters. Selection, connection, reconnection and an operator grant never submit commands automatically.

The Commands panel labels Physical commands or Simulated commands. Available actions appear in the selector. Expand unavailable actions to read their reasons.

Forms show units, limits and saved choices. Furnace values start empty. Enter temperatures in °C, ramps in °C/min and gas flow in percent as specified by the schema. The form checks sequence JSON against its schema. Advanced JSON and parameter schema shows the complete schema. Optional Preconditions (JSON) starts with `{}`.

Move one stage axis uses an absolute encoder position in rotations, measured from the selected axis zero. The portal shows the selected axis current position and calibrated minimum and maximum. Missing or outdated calibration data disables Send command. A calibration refresh does not replace the target you entered.

The `/commands/choices` response includes `constraints.stage` from the existing device cache. These runtime limits do not change the catalog schema or its digest. The server rechecks the selected axis limits, faults, lease and Stop epoch before movement. X/Y moves do not depend on the Insert position. This applies to single-axis moves, combined coordinates, jogs, steps and saved positions. Combined X/Y moves use Bottom, then Top, and leave Insert unchanged. Named positions use Bottom, Top, then Insert without an extra extraction move. Only 1 axis moves at a time.

Rejected and failed commands show their reason beside the command controls. Expand Recent commands and results for the full record.

Each deliberate Send command creates a new `request_id`. If the response is lost, the portal queries that ID without repeating the command. An unresolved result blocks another submission. Changing sources clears the command view and ignores late responses from the previous source.

### Handover and connection loss

Take control returns ownership to the human. Release control leaves no operator. Revoke access disconnects the agent and prevents reconnection. Revoked agents disappear from the Agent connections list. Internal access records remain available for credential rejection and command or Stop attribution. Approved agents remain listed when disconnected. These ownership changes require idle equipment.

Each change updates `control_epoch`. The command worker cancels obsolete work and releases only that agent's device leases. It does not acquire another holder's lease during cleanup.

After Connect, the local portal process sends a heartbeat every 2 seconds. Minimizing or closing the browser does not end the agent connection. Keep the portal process running, the PC awake and LarePass connected. Explicit Disconnect stops the local agent session. Revocation, session expiry or a Console restart also ends that session. Reopening the page shows the current session without sending equipment commands.

The connected local agent also collects 1 raw state snapshot per second without an open browser. It keeps only the latest snapshot in memory. This receipt check does not decode or verify the data agreement. Status marks it unverified. The browser visualizer keeps its separate verified stream. `GET /api/agent-control/latest?source=live` returns the local receipt status and last raw snapshot. Disconnect stops collection. A network error marks the retained snapshot as last received.

After 10 seconds without a heartbeat, the session and grant expire. The Console records a warning for the lost operator. Reconnection starts as an observer and requires another explicit grant.

The watchdog uses `policy: "warn_only"` and `shutdown_enabled: false`. It adds no automatic furnace shutdown. Cleanup releases motion control. The furnace follows its existing run/recovery behavior.

The warning includes the sender and remains visible until a human selects Dismiss warning. This sends `POST /lab/api/agent/control/acknowledge-warning` with `{}`. Dismissal does not restore authority or remove the recorded event.

Client access records and `last_connected_at` persist in `agent-connect/authority.db` under the Console runs directory. This timestamp records the latest successful connection or reconnection in Unix seconds. It is `null` if no connection time is recorded, including historical records. Pairing, heartbeats, status reads, control release and rejected connections do not update it. Invitations, sessions and grants do not persist. Restarting the Console invalidates sessions and grants.

The PC portal keeps Console connection credentials only in server memory. Restarting the portal requires a new invitation, even when a model profile remains active. A legacy pending client must use a new invitation.

Start test, Run next step and Stop test provide a separate memory-only handover test. The test has 3 manually triggered steps, including on Olares. Stop test affects only this test. It does not stop equipment.

### Command API and results

Command routes use the `/lab/api/agent/control` base path:

| Method | Path | Access and result |
| --- | --- | --- |
| GET | `/commands/catalog` | Public. Read definitions, availability and the digest. |
| GET | `/commands/choices` | Authenticated. Read saved profile and device choices. |
| GET | `/commands/history` | Authenticated. Read the last 50 summaries and total count. |
| GET | `/commands/request/<request_id>` | Authenticated. Read the full record. |
| POST | `/commands` | Authenticated. New commands require the current operator and contract. |
| POST | `/commands/cancel` | Authenticated. Send `{"request_id":"..."}` for an owned operation. |

Agents use `Authorization: Bearer <session_token>`. The portal supplies its token from server memory. The browser cannot replace it. Agents can read only their own client's records. Trusted Console management can read all records. If an ID matches multiple clients, a management lookup returns 409.

Catalog version 1 contains `version`, `contract_id` and `actions`. The digest starts with `sha256:`. Each action includes `id`, `label`, `description`, `params`, `defaults`, `device`, `available`, `reason` and `revision`. Availability can change without changing the definition digest.

Submit only `request_id`, `boot_id`, `contract_id`, `control_epoch`, `action` and `params`, plus optional `preconditions`. The last 2 fields must be JSON objects. The portal sends `preconditions` explicitly. The server checks the current identity, command digest, operator grant and action schema before dispatch.

Use control status `command_boot_id` as the command's `boot_id`. Its `identity_scope` is `command_control`. Keep `control_epoch` as the exact returned string. Status `command_contract_id` must match catalog `contract_id`. The read stream has a separate `boot_id` and agreement digest. Do not substitute them in a command.

Request IDs belong to a client. With a current session, an identical ID and body return the saved record without dispatch. A different body under the same ID returns 409. An expired session cannot read or repeat a request. For an unknown result, query the original ID. Do not replay movement with a new ID automatically.

Operations report `accepted`, `running`, `succeeded`, `failed`, `rejected`, `cancelling`, `stopped` or `unknown`. Cancel my operation requests cancellation of this session's operation. It does not prove equipment stopped. Furnace cancellation preserves its run/recovery behavior. Use the explicit furnace or gas Stop controls when those outputs must stop.

Records persist in `agent-connect/commands.db` beside the authority database. Restart changes unfinished records to `unknown` without replay. Local simulation uses memory or temporary storage. Restarting local tests clears their records and profiles.

History returns `items` and `count`. Summaries omit parameters and results. Expand an operation to read its full record.

### Stop and sender history

Stop uses the same control base path:

| Method | Path | Result |
| --- | --- | --- |
| POST | `/stop` | Dispatch Stop and return the initial record. |
| GET | `/stops` | Read bounded summaries and audit status. |
| GET | `/stops/<id>` | Read delivery details and device outcomes. |

Send only a request ID and target:

```json
{"request_id":"example-stop-1","target":"all_motion"}
```

| Target | Effect |
| --- | --- |
| `stage`, `robot`, `microscope` | Stop the selected motion device. |
| `all_motion` | Stop those motion devices. It excludes the furnace. |
| `furnace` | Request the native furnace Stop for heat and gas. |
| `gas_all` | Stop all 3 gas channels. |
| `gas_0`, `gas_1`, `gas_2` | Stop gas channel 0, 1 or 2. |

Stop requires no session, operator grant, current epoch or contract digest. It does not wait for normal command handling or a status refresh. The server resolves the sender. Callers cannot set a sender in the body. If authority is available, a request without `Authorization` can be attributed to the trusted human. Unrecognized credentials or unavailable attribution produce an unverified sender. An agent sends its token when available, but missing access must not block Stop.

Each deliberate press creates a new ID. Copies with the same ID and target share a history record, but each delivery still dispatches Stop. Details retain every transport, sender and device outcome. A mixed-sender request does not inherit another delivery's verified identity.

Stop history reports `pending`, `confirmed`, `unconfirmed`, `unknown` or `disabled`. Receipt does not prove the device stopped. An unknown delivery remains unknown after another delivery succeeds. Existing human emergency routes preserve their dispatch behavior and add sender history after dispatch.

The Console saves history in `runs/agent-connect/stops.db`. Audit storage cannot delay Stop dispatch. History exposes storage failures. A process exit before saving can lose recent records. Restart changes unfinished deliveries to `unknown`. Saved Stops never replay.

### Independent Stop gateway

A network failure or HTTP 503 from the normal Stop route triggers the fixed fallback. The portal preserves the request ID and sends no credentials:

| Source | Fallback base |
| --- | --- |
| Live | `http://100.64.0.1:8082/lab/api/agent/emergency` |
| Simulation | `http://127.0.0.1:8794` |

Both expose `/stop`, `/status`, `/stops` and `/stops/<id>`. Fallback callers are unverified because this process has no Console session store. History retains both Console and fallback records after recovery. A service or network failure can still leave Stop unconfirmed. The fallback does not replay normal commands or replace live readings with simulated data.

The Olares gateway listens at `127.0.0.2:8874`. The 8082 proxy forwards `/lab/api/agent/emergency/*` to it. The physical deployment runs `orchestrator.agent_connect.fallback --physical` and saves `runs/agent-connect/emergency-stops.db`. This mode exposes fixed native Stop and status handlers only. Normal physical commands remain absent from the gateway.

The library gateway remains disabled by default. Generate the physical unit with `service_unit(checkout, physical=True)`. Install it as `mg400-agent-emergency-test.service`, then enable it for host startup:

```sh
sudo systemctl enable --now mg400-agent-emergency-test.service
```

The normal `deploy/twin.py` registry does not restart this unit. After gateway code changes, restart it separately:

```sh
sudo systemctl restart mg400-agent-emergency-test.service
```

Check emergency status for `equipment_mode: "physical"` and `stop_enabled: true`. This gateway is separate from the Console process. It still depends on the proxy, network and device services. It does not change the 8080 Console or restart shared device processes.


## Optional simulation tests

Enable simulation tests is off by default. Enable it to stop the live reader and open the test section. Disable it to close the test connections and resume the live reader. The tests make no background reads while disabled. Only the selected test tab runs.

The optional tests use the Simulated Console at `http://127.0.0.1:8790/lab/`. Open it from the link in the test section. The launcher does not open it automatically. The test source shows `MOCK`. Test controls change only local simulation data and never contact Olares.

The mock adapter uses a fixed loopback address and ignores proxy variables. It rejects redirects and non-mock discovery responses.

### Data format tests

Select Data format tests for the compatibility fixtures under `/lab/api/agent-fixtures`.

When updates run, the Simulated Console supplies changing stage positions and temperatures. It also contains 2 chip examples. Earlier agent version (A) and Newer agent version (B) are scripted tests. Each keeps a separate copy of the data agreement: the message format, fields and units. The newer script supports the added optional field.

1. Open the Simulated Console. Inspect discovery, received readings and the verified data agreement in Data format tests.
2. Select Pause updates. A reading older than 3 seconds becomes Outdated reading. Polling preserves its original sample time.
3. Select Resume updates to receive new samples. Simulate old readings pauses updates and immediately makes the current sample outdated.
4. Choose a test setup. Compare the current and saved agreement digests, changed definitions and each script's compatibility result.
5. Disconnect a script. Reconnect it after Simulate Console restart to inspect the new process ID.
6. Run the basic scenarios. Export received messages, agreement files and expected/actual results as JSON.

| Test setup | Check |
| --- | --- |
| Original format (1.0) | Use the original definitions. |
| Add optional field (1.1) | Accept a compatible addition. |
| Change position units (2.0) | Detect changed units. |
| Unrecognized schema format | Reject an unsupported schema dialect. |
| Damaged agreement file | Reject a file whose digest does not match. |

Change coordinate reference increments the stage reference revision and preserves its position. Simulate Console restart changes the Console and stage process IDs, resets the sequence and increments the reference revision. It preserves readings and chip records. Reset demo restores the original format, starting readings and chip records, then resumes updates. It also resets the sequence, reference revision and source history and creates new process IDs.

The scenario runner uses a separate disposable source. It does not reset the interactive source. The export retains bounded message history. Restarting the portal clears its in-memory records.

### Samples and stream tests

Samples & stream uses `/lab/api/agent`. Connection A and Connection B are independent browser WebSocket connections. Each verifies its agreement digest and retains its own replay cursor. Choose device, chip or operation groups and a rate of 1, 2 or 4 Hz. The default is 2 Hz. Enter exact chip IDs on separate lines, or select chips with work in progress. Apply the selection to both connections.

Use Read slowly on either connection while the other continues. Disconnect and reconnect a connection to resume from its cursor. Simulate restart changes the process identity. Expire saved events removes replay history. The connection reports missing history and reads a fresh snapshot. Agreement changes trigger discovery. An unfamiliar frame digest also triggers discovery if the change event was missed. Unsupported definitions stop decoded readings.

Browse saved chips reads a paged list. If the sample revision changes between pages, start again with Browse saved chips. Manual reports and Mark location unknown change only the mock sample store. The evidence buttons simulate pending picks, inferred placement, camera continuity, old frames and conflicting identity. Simulated furnace entry uses a test mapping. Losing its reference preserves the stage holder and makes entry unknown.

## Sample evidence

Phase 2 stores ordered `chip_events` and `sample_current` rows in the existing `runs.db`. Event insertion, current state and revision updates share a transaction. The existing run measurements in `samples` remain unchanged. Reads do not create events or current rows. Current rows can be rebuilt from chip events.

Chip IDs remain exact. Historical run, step, measurement, artifact and event records identify chips but do not establish their current location. An imported chip starts with an unknown location. Its latest run and step refer only to started work for that chip. A newer unstarted run or another chip's work does not replace them.

The evidence basis is `observed`, `reported`, `inferred` or `unknown`. Acceptance does not prove arrival. A handoff can make the holder unknown while preserving the last known location and pending destination. A confirmed rejection before motion preserves the previous holder. Old frames cannot restore an outdated location. Conflicts make the disputed claim unknown. Source observation time stays separate from record update time.

The holder and furnace entry are separate claims. Losing an entry reference preserves an established stage holder. No live furnace mapping is configured, so `inside_furnace` remains `null` on Olares. A mock mapping does not establish a live location.

The chip passport reads `/api/chip/location?id=...` under the Console base path. It shows current evidence beside existing history. Live Report location and Mark unknown remain disabled. The local mock form remains the only manual-report interface.

## Read API and replay

The Console publishes these read routes under `/lab/api/agent`:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/discovery` | Read process identity, current agreement and endpoint paths. |
| GET | `/contracts/{digest}.json` | Read an immutable JSON Schema 2020-12 bundle. |
| GET | `/v1/state` | Read a selected current snapshot. |
| GET | `/v1/samples` | Read a bounded sample page. |
| WebSocket | `/v1/stream` | Receive current state and ordered events. Sending commands is not supported. |

State and stream options are `groups`, `rate_hz`, `selection` and repeated `chip_id` fields. Groups are `devices`, `samples` and `operations`. Select `active` or `ids`. Active chips have a pending operation or started work in a running or paused run. Explicit selection accepts at most 50 IDs, each at most 256 characters. Use repeated `chip_id` fields for exact IDs containing commas. The alternate `chip_ids` field splits IDs at commas.

Sample pages also accept `selection=all`, `limit`, `offset` and `expected_revision`. The maximum page size is 100. Preserve the returned revision in `expected_revision` for subsequent pages. A revision mismatch requires pagination to restart. Read `count`, `total`, `complete` and `next_offset`. Completeness applies only to the selected chips.

The shared producer assembles frames at 4 Hz. Subscribers receive full frames at 1, 2 or 4 Hz. Device measurements can update more slowly. Source time and age remain separate from frame time. Subscribers read the shared device cache and do not add hardware polling. State, replay, subscriber counts and message sizes have fixed bounds. A slow reader keeps the newest unsent state rather than accumulating old frames.

One shared reader reads thermal summaries at 1 Hz. It reads the existing latest vision result once when the job changes. It starts no capture jobs. All subscribers reuse these summaries without separate hardware polling.

Older vision results have `unknown` quality for the present scene. Capture time remains `null` when the source omits it.

Reconnect with both `boot_id` and `event_seq`. The stream sends a current snapshot before retained events. Events retain their original `contract_id`, so replay does not change the current agreement. A `resync` message reports `boot_changed`, `cursor_ahead` or `history_evicted`. Read a fresh snapshot after a gap. The shared sampled history remains separate from repeated broadcasts.

## Cached fields and release identity

Each device adds `cached_state` with `values`, `units`, `complete`, `omitted` and `omitted_count`. These fields expose bounded native cached values without extra hardware polling. The copy excludes private data, paths, images and thermal matrices. Size limits can omit more fields. Read omission reasons before treating the copy as complete.

Device `data_gaps` names missing source identity or timestamps. Unknown values remain unknown. The portal shows the received nested fields and raw JSON. A current Console frame does not make an old sensor reading current. Source units and observation age remain separate from frame generation time.

Discovery `release` contains `release_id`, `source_bundle_id`, `read_contract_id`, `command_contract_id`, `source`, `complete` and `scope`. The scope is `this-console`. The release ID combines the startup source digest with both current contract digests. The source digest covers a bounded set of Console source files, not device firmware or every shared service.

The `source` object reports `digest`, `complete`, `file_count`, `bytes_hashed` and `scope`. The running process keeps its startup source identity even if files change on disk. Missing metadata or an incomplete source copy remains incomplete. Compare the release ID and both contract IDs when identifying a reviewed Console release.

## Current boundary

The catalog limits agent operation to reviewed native capabilities. Enabling physical operation does not enable an unsupported action or remove device limits. The live reader, simulated equipment and format fixtures retain separate identities and results. Simulated checks do not establish physical-device behavior.

Further device-service process-ID integration and deployment remain deferred until the other developer completes shared-service work. Reconcile the retained branch implementation at the next joint integration review. Missing service IDs remain unavailable. Console `boot_id`, client identity and `control_epoch` remain active.
